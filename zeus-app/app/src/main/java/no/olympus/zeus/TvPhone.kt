package no.olympus.zeus

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.net.wifi.WifiManager
import android.os.IBinder
import androidx.mediarouter.media.MediaRouteSelector
import androidx.mediarouter.media.MediaRouter
import com.google.android.gms.cast.CastDevice
import com.google.android.gms.cast.CastMediaControlIntent
import com.google.android.gms.cast.MediaInfo
import com.google.android.gms.cast.MediaLoadRequestData
import com.google.android.gms.cast.MediaMetadata
import com.google.android.gms.cast.MediaSeekOptions
import com.google.android.gms.cast.framework.CastContext
import com.google.android.gms.cast.framework.CastOptions
import com.google.android.gms.cast.framework.OptionsProvider
import com.google.android.gms.cast.framework.SessionProvider
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import java.io.BufferedReader
import java.io.File
import java.io.InputStreamReader
import java.io.OutputStream
import java.io.RandomAccessFile
import java.net.DatagramPacket
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.MulticastSocket
import java.net.ServerSocket
import java.net.Socket
import java.net.URL
import java.security.SecureRandom
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.TimeUnit
import kotlin.concurrent.thread

/** Casting borte: telefonen finner TV-ene på nettet den står på (Chromecast/Google TV via Cast, Samsung/LG o.l.
 *  via DLNA), henter filmen fra PC-en over Tailscale og sender den videre til TV-en (Relay). Hjemme styrer PC-en
 *  TV-ene selv (olympus/cast.py) – samme knapp, appen velger vei. */
data class PhoneTv(val id: String, val name: String, val kind: String, val model: String,
                   val route: MediaRouter.RouteInfo? = null, val control: String = "", val ip: String = "")

class CastOptions_ : OptionsProvider {
    override fun getCastOptions(context: Context): CastOptions =
        CastOptions.Builder().setReceiverApplicationId(CastMediaControlIntent.DEFAULT_MEDIA_RECEIVER_APPLICATION_ID).build()

    override fun getAdditionalSessionProviders(context: Context): List<SessionProvider>? = null
}

object PhoneCast {
    private val selector = MediaRouteSelector.Builder()
        .addControlCategory(CastMediaControlIntent.categoryForCast(CastMediaControlIntent.DEFAULT_MEDIA_RECEIVER_APPLICATION_ID)).build()
    // aldri følg omdirigeringer: en enhet skal ikke kunne sende telefonen videre forbi sameHost-sjekken
    private val http = OkHttpClient.Builder().connectTimeout(3, TimeUnit.SECONDS).readTimeout(8, TimeUnit.SECONDS)
        .followRedirects(false).followSslRedirects(false).build()
    var now: PhoneTv? = null
        private set
    private var nowStart = 0.0
    private var nowVideo: Video? = null

    suspend fun discover(context: Context): List<PhoneTv> {
        val cast = runCatching { castRoutes(context) }.getOrDefault(emptyList())
        val dlna = runCatching { dlna(context) }.getOrDefault(emptyList())
        val castHosts = cast.map { it.name }.toSet()
        return cast + dlna.filter { it.name !in castHosts }
    }

    private suspend fun castRoutes(context: Context): List<PhoneTv> = withContext(Dispatchers.Main) {
        CastContext.getSharedInstance(context)  // starter Cast-rammeverket
        val router = MediaRouter.getInstance(context)
        val cb = object : MediaRouter.Callback() {}
        router.addCallback(selector, cb, MediaRouter.CALLBACK_FLAG_PERFORM_ACTIVE_SCAN)
        delay(4000)
        val routes = router.routes.filter { !it.isDefault && it.matchesSelector(selector) && it.isEnabled }
        router.removeCallback(cb)
        routes.map { r ->
            val dev = CastDevice.getFromBundle(r.extras)
            PhoneTv("pcast:${dev?.deviceId ?: r.id}", r.name.toString(), "chromecast", dev?.modelName.orEmpty(), route = r,
                ip = dev?.ipAddress?.hostAddress.orEmpty())
        }
    }

    // --- DLNA (SSDP + SOAP), samme regler som på PC-en: lenker må peke på enheten som svarte ---
    private suspend fun dlna(context: Context): List<PhoneTv> = withContext(Dispatchers.IO) {
        val wifi = context.applicationContext.getSystemService(WifiManager::class.java)
        val lock = wifi?.createMulticastLock("zeus-ssdp")?.apply { setReferenceCounted(false); acquire() }
        val found = LinkedHashMap<String, String>()  // vert → beskrivelse
        try {
            MulticastSocket(null as java.net.SocketAddress?).use { s ->
                s.reuseAddress = true; s.bind(InetSocketAddress(0)); s.soTimeout = 600
                val msg = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\nMX: 2\r\n" +
                    "ST: urn:schemas-upnp-org:device:MediaRenderer:1\r\n\r\n").toByteArray()
                s.send(DatagramPacket(msg, msg.size, InetAddress.getByName("239.255.255.250"), 1900))
                val end = System.currentTimeMillis() + 3000
                val buf = ByteArray(4096)
                while (System.currentTimeMillis() < end && found.size < 32) {
                    val p = DatagramPacket(buf, buf.size)
                    try { s.receive(p) } catch (e: java.net.SocketTimeoutException) { continue }
                    val host = p.address.hostAddress ?: continue
                    val loc = Regex("(?im)^location:\\s*(\\S{1,300})").find(String(p.data, 0, p.length))?.groupValues?.get(1) ?: continue
                    if (sameHost(loc, host) && host !in found) found[host] = loc
                }
            }
        } finally { lock?.release() }
        found.mapNotNull { (host, loc) -> runCatching { describe(host, loc) }.getOrNull() }
    }

    private fun sameHost(url: String, host: String): Boolean = runCatching {
        val u = URL(url)
        u.protocol == "http" && u.host == host && InetAddress.getByName(host).isSiteLocalAddress
    }.getOrDefault(false)

    private fun describe(host: String, loc: String): PhoneTv? {
        val xml = http.newCall(Request.Builder().url(loc).build()).execute().use { r ->
            if (!r.isSuccessful || r.request.url.host != host) return null
            r.body?.source()?.let { src -> src.request(200_000); src.buffer.readUtf8(minOf(src.buffer.size, 200_000)) }.orEmpty()
        }
        val name = Regex("<friendlyName>([^<]*)").find(xml)?.groupValues?.get(1)?.trim() ?: host
        val model = Regex("<modelName>([^<]*)").find(xml)?.groupValues?.get(1)?.trim().orEmpty()
        for (svc in Regex("<service>(.*?)</service>", RegexOption.DOT_MATCHES_ALL).findAll(xml)) {
            if ("AVTransport" !in svc.value) continue
            val ctl = Regex("<controlURL>([^<]*)").find(svc.value)?.groupValues?.get(1)?.trim() ?: continue
            val control = URL(URL(loc), ctl).toString()
            if (sameHost(control, host)) return PhoneTv("pdlna:$host", clean(name), "dlna", clean(model), control = control, ip = host)
        }
        return null
    }

    private fun clean(s: String) = s.replace(Regex("[^\\w .,'()&+\\-]"), "").trim().take(60).ifEmpty { "TV" }

    private fun soap(tv: PhoneTv, action: String, args: String = "") {
        val avt = "urn:schemas-upnp-org:service:AVTransport:1"
        val env = "<?xml version=\"1.0\" encoding=\"utf-8\"?><s:Envelope xmlns:s=\"http://schemas.xmlsoap.org/soap/envelope/\" " +
            "s:encodingStyle=\"http://schemas.xmlsoap.org/soap/encoding/\"><s:Body><u:$action xmlns:u=\"$avt\">" +
            "<InstanceID>0</InstanceID>$args</u:$action></s:Body></s:Envelope>"
        http.newCall(Request.Builder().url(tv.control).header("SOAPAction", "\"$avt#$action\"")
            .post(env.toRequestBody("text/xml; charset=\"utf-8\"".toMediaType())).build()).execute().use { r ->
            if (r.code >= 300) throw java.io.IOException("${tv.name} avviste $action (${r.code})")
        }
    }

    private fun esc(s: String) = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\"", "&quot;")
    private fun hms(sec: Double) = sec.toLong().coerceAtLeast(0).let { "%d:%02d:%02d".format(it / 3600, it / 60 % 60, it % 60) }

    // --- avspilling ---
    suspend fun play(context: Context, hub: Hub, tv: PhoneTv, v: Video, start: Double) {
        val plan = hub.api.get("/media/video/${v.id}/tvplan?kind=${tv.kind}&model=${java.net.URLEncoder.encode(tv.model, "UTF-8")}")
        val mode = plan.optString("mode")
        val mime = plan.optString("mime", "video/mp4")
        val ext = plan.optString("ext", ".mp4")
        // Nedlastet kopi først (uten PC og nett hjemme) – når TV-en spiller fila som den er
        val local = if (mode == "direct" && Offline.has(context, v.id)) Offline.file(context, v.id) else null
        val streamStart = if (mode == "direct") 0.0 else start
        val sub = plan.optJSONObject("sub")
        val subVtt = sub?.let { Relay.add(context, hub, "/media/video/${v.id}/sub/${it.optInt("idx")}?start=$streamStart", null, ".vtt", "text/vtt", tv.ip) }
        val subSrt = sub?.let { Relay.add(context, hub, "/media/video/${v.id}/sub/${it.optInt("idx")}?fmt=srt&start=$streamStart", null, ".srt", "application/x-subrip", tv.ip) }
        val url = Relay.add(context, hub, "/media/video/${v.id}/tvstream?kind=${tv.kind}&model=${java.net.URLEncoder.encode(tv.model, "UTF-8")}&start=$streamStart", local, ext, mime, tv.ip)
        RelayService.start(context, tv.name, v.title)
        val seek = if (mode == "direct") start else 0.0
        if (tv.kind == "chromecast") castLoad(context, tv, url, mime, v.title, seek, subVtt, sub?.optString("lang"))
        else withContext(Dispatchers.IO) {
            runCatching { soap(tv, "Stop") }
            val didl = esc("<DIDL-Lite xmlns=\"urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/\" xmlns:dc=\"http://purl.org/dc/elements/1.1/\" " +
                "xmlns:upnp=\"urn:schemas-upnp-org:metadata-1-0/upnp/\" xmlns:sec=\"http://www.sec.co.kr/\"><item id=\"0\" parentID=\"-1\" restricted=\"1\"><dc:title>${esc(v.title)}</dc:title>" +
                "<upnp:class>object.item.videoItem.movie</upnp:class><res protocolInfo=\"http-get:*:$mime:*\">${esc(url)}</res>" +
                (subSrt?.let { "<res protocolInfo=\"http-get:*:text/srt:*\">${esc(it)}</res><sec:CaptionInfoEx sec:type=\"srt\">${esc(it)}</sec:CaptionInfoEx>" } ?: "") +
                "</item></DIDL-Lite>")
            soap(tv, "SetAVTransportURI", "<CurrentURI>${esc(url)}</CurrentURI><CurrentURIMetaData>$didl</CurrentURIMetaData>")
            soap(tv, "Play", "<Speed>1</Speed>")
            if (seek > 1) { delay(2000); runCatching { soap(tv, "Seek", "<Unit>REL_TIME</Unit><Target>${hms(seek)}</Target>") } }
        }
        now = tv; nowStart = streamStart; nowVideo = v
    }

    private suspend fun castLoad(context: Context, tv: PhoneTv, url: String, mime: String, title: String, seek: Double,
                                 subUrl: String? = null, subLang: String? = null) {
        val ctx = withContext(Dispatchers.Main) { CastContext.getSharedInstance(context) }
        withContext(Dispatchers.Main) { MediaRouter.getInstance(context).selectRoute(tv.route ?: error(t("TV-en forsvant"))) }
        val session = withTimeoutOrNull(20_000) {
            var s = withContext(Dispatchers.Main) { ctx.sessionManager.currentCastSession }
            while (s?.remoteMediaClient == null) { delay(300); s = withContext(Dispatchers.Main) { ctx.sessionManager.currentCastSession } }
            s
        } ?: throw java.io.IOException(t("Fikk ikke kontakt med %s", tv.name))
        withContext(Dispatchers.Main) {
            val meta = MediaMetadata(MediaMetadata.MEDIA_TYPE_MOVIE).apply { putString(MediaMetadata.KEY_TITLE, title) }
            val tracks = subUrl?.let { listOf(com.google.android.gms.cast.MediaTrack.Builder(1, com.google.android.gms.cast.MediaTrack.TYPE_TEXT)
                .setContentId(it).setContentType("text/vtt").setSubtype(com.google.android.gms.cast.MediaTrack.SUBTYPE_SUBTITLES)
                .setLanguage(subLang ?: "und").setName("Undertekst").build()) }
            val info = MediaInfo.Builder(url).setContentType(mime).setStreamType(MediaInfo.STREAM_TYPE_BUFFERED).setMetadata(meta)
                .apply { tracks?.let { setMediaTracks(it) } }.build()
            session.remoteMediaClient!!.load(MediaLoadRequestData.Builder().setMediaInfo(info).setAutoplay(true)
                .setCurrentTime((seek * 1000).toLong()).apply { if (tracks != null) setActiveTrackIds(longArrayOf(1)) }.build())
        }
    }

    /** pause | resume | stop | seek. Omkodet/ompakket strøm spoles ved å starte på nytt derfra (som hjemme). */
    suspend fun control(context: Context, hub: Hub, action: String, position: Double = 0.0) {
        val tv = now ?: return
        if (action == "seek" && nowStart >= 0 && tv.kind == "chromecast") {
            val v = nowVideo
            val plan = v?.let { runCatching { hub.api.get("/media/video/${it.id}/tvplan?kind=${tv.kind}&model=${java.net.URLEncoder.encode(tv.model, "UTF-8")}") }.getOrNull() }
            if (v != null && plan?.optString("mode") != "direct") { play(context, hub, tv, v, position); return }
        }
        if (tv.kind == "chromecast") withContext(Dispatchers.Main) {
            val rmc = CastContext.getSharedInstance(context).sessionManager.currentCastSession?.remoteMediaClient ?: return@withContext
            when (action) {
                "pause" -> rmc.pause(); "resume" -> rmc.play(); "stop" -> rmc.stop()
                "seek" -> rmc.seek(MediaSeekOptions.Builder().setPosition((position * 1000).toLong()).build())
            }
            if (action == "stop") CastContext.getSharedInstance(context).sessionManager.endCurrentSession(true)
        } else withContext(Dispatchers.IO) {
            when (action) {
                "pause" -> soap(tv, "Pause"); "resume" -> soap(tv, "Play", "<Speed>1</Speed>"); "stop" -> soap(tv, "Stop")
                "seek" -> soap(tv, "Seek", "<Unit>REL_TIME</Unit><Target>${hms(position)}</Target>")
            }
        }
        if (action == "stop") { now = null; nowVideo = null; Relay.clear(); RelayService.stop(context) }
    }

    /** Posisjon i sekunder (bare Chromecast rapporterer det enkelt). */
    fun position(context: Context): Double? = runCatching {
        val rmc = CastContext.getSharedInstance(context).sessionManager.currentCastSession?.remoteMediaClient ?: return null
        rmc.approximateStreamPosition / 1000.0 + nowStart
    }.getOrNull()

    fun paused(context: Context): Boolean = runCatching {
        CastContext.getSharedInstance(context).sessionManager.currentCastSession?.remoteMediaClient?.isPaused == true
    }.getOrDefault(false)
}

/** Videresenderen: en liten HTTP-server på telefonens Wi-Fi-adresse. TV-en henter /r/<nøkkel>/video.*, telefonen
 *  henter fra PC-en over Tailscale (eller fra en nedlastet kopi) og sender videre, med Range for spoling. */
object Relay {
    // Videresenderen står på et fremmed nett: bare TV-en den sender til slipper inn (allow = TV-ens IP), nøkkelen
    // går ut etter 6 t, og høyst 8 samtidige tilkoblinger med små, begrensede hoder.
    private class Entry(val path: String, val local: File?, val mime: String, val allow: String, val expires: Long)
    private val entries = ConcurrentHashMap<String, Entry>()
    private val open = java.util.concurrent.Semaphore(8)
    private var server: ServerSocket? = null
    private var hub: Hub? = null

    fun add(context: Context, hub: Hub, path: String, local: File?, ext: String, mime: String = "video/mp4", allow: String = ""): String {
        this.hub = hub
        val ip = wifiIp(context) ?: throw java.io.IOException(t("Telefonen er ikke på et Wi-Fi-nett TV-en kan nå"))
        val s = server ?: ServerSocket(0, 16, InetAddress.getByName(ip)).also { srv ->
            server = srv
            thread(isDaemon = true, name = "zeus-relay") {
                while (!srv.isClosed) runCatching {
                    val c = srv.accept()
                    // andre enn TV-en avvises før de får en av de 8 plassene (ellers kunne tomme tilkoblinger
                    // fra noen på nettet stenge TV-en ute)
                    val from = c.inetAddress.hostAddress
                    val allowed = entries.values.any { it.allow.isEmpty() || it.allow == from }
                    if (!allowed || !open.tryAcquire()) { c.close(); return@runCatching }
                    thread(isDaemon = true) { try { serve(c) } finally { open.release() } }
                }
            }
        }
        val tok = ByteArray(18).also { SecureRandom().nextBytes(it) }.joinToString("") { "%02x".format(it) }
        entries[tok] = Entry(path, local, mime, allow, System.currentTimeMillis() + 6 * 3600_000L)
        return "http://$ip:${s.localPort}/r/$tok/video$ext"
    }

    fun clear() { entries.clear(); runCatching { server?.close() }; server = null }

    private fun wifiIp(context: Context): String? {
        val cm = context.getSystemService(ConnectivityManager::class.java) ?: return null
        val net = cm.allNetworks.firstOrNull { cm.getNetworkCapabilities(it)?.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) == true } ?: return null
        return cm.getLinkProperties(net)?.linkAddresses?.map { it.address }?.firstOrNull { it is java.net.Inet4Address && it.isSiteLocalAddress }?.hostAddress
    }

    private fun serve(c: Socket) = c.use { sock ->
        sock.soTimeout = 5_000  // forespørselen må komme raskt; under strømmingen settes den høyere
        val reader = BufferedReader(InputStreamReader(sock.getInputStream()), 8192)
        fun line(): String? = reader.readLine()?.take(2048)
        val first = line() ?: return
        val parts = first.split(" ")
        val headers = generateSequence { line()?.takeIf { it.isNotEmpty() } }.take(50).toList()
        val range = headers.firstOrNull { it.startsWith("range:", true) }?.substringAfter(":")?.trim()
        val tok = Regex("^/r/([0-9a-f]+)/").find(parts.getOrElse(1) { "" })?.groupValues?.get(1)
        val e = tok?.let { entries[it] }?.takeIf {
            it.expires > System.currentTimeMillis() && (it.allow.isEmpty() || it.allow == sock.inetAddress.hostAddress)
        }
        sock.soTimeout = 60_000
        val out = sock.getOutputStream()
        if (e == null || parts[0] !in listOf("GET", "HEAD")) { out.write("HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n".toByteArray()); return }
        if (e.local != null) serveFile(out, e.local, e.mime, range, parts[0] == "HEAD") else proxy(out, e.path, range, parts[0] == "HEAD")
    }

    private fun serveFile(out: OutputStream, f: File, mime: String, range: String?, head: Boolean) {
        val len = f.length()
        val m = range?.let { Regex("bytes=(\\d*)-(\\d*)").find(it) }
        val from = m?.groupValues?.get(1)?.toLongOrNull() ?: 0L
        val to = m?.groupValues?.get(2)?.toLongOrNull()?.coerceAtMost(len - 1) ?: (len - 1)
        val status = if (m != null) "206 Partial Content" else "200 OK"
        out.write(("HTTP/1.1 $status\r\nContent-Type: $mime\r\nAccept-Ranges: bytes\r\nContent-Length: ${to - from + 1}\r\n" +
            (if (m != null) "Content-Range: bytes $from-$to/$len\r\n" else "") + "Connection: close\r\n\r\n").toByteArray())
        if (head) return
        RandomAccessFile(f, "r").use { raf ->
            raf.seek(from); var left = to - from + 1; val buf = ByteArray(256 * 1024)
            while (left > 0) { val n = raf.read(buf, 0, minOf(buf.size.toLong(), left).toInt()); if (n < 0) break; out.write(buf, 0, n); left -= n }
        }
    }

    private fun proxy(out: OutputStream, path: String, range: String?, head: Boolean) {
        val api = hub?.api ?: return
        api.stream(path, range).use { r ->
            val keep = listOf("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges")
            val sb = StringBuilder("HTTP/1.1 ${r.code} ${r.message.ifEmpty { "OK" }}\r\n")
            keep.forEach { h -> r.header(h)?.let { sb.append("$h: $it\r\n") } }
            sb.append("Access-Control-Allow-Origin: *\r\nConnection: close\r\n\r\n")  // Chromecast henter undertekster med CORS
            out.write(sb.toString().toByteArray())
            if (head) return
            r.body?.byteStream()?.use { it.copyTo(out, 256 * 1024) }
        }
    }
}

/** Holder telefonen våken mens den videresender film til en TV (ellers stopper Android den i bakgrunnen). */
class RelayService : Service() {
    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val nm = getSystemService(NotificationManager::class.java)
        nm?.createNotificationChannel(NotificationChannel("tv", t("Casting til TV"), NotificationManager.IMPORTANCE_LOW))
        val n = Notification.Builder(this, "tv").setSmallIcon(R.drawable.ic_zeus_fg)
            .setContentTitle(t("▭ %s", intent?.getStringExtra("title").orEmpty()))
            .setContentText("sendes til ${intent?.getStringExtra("tv").orEmpty()} via telefonen").setOngoing(true).build()
        startForeground(77, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PLAYBACK)
        return START_NOT_STICKY
    }

    companion object {
        fun start(context: Context, tv: String, title: String) =
            context.startForegroundService(Intent(context, RelayService::class.java).putExtra("tv", tv).putExtra("title", title))

        fun stop(context: Context) = context.stopService(Intent(context, RelayService::class.java))
    }
}
