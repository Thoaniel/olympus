package no.olympus.zeus

import android.content.ComponentName
import android.content.Context
import android.net.Uri
import android.os.Bundle
import androidx.annotation.OptIn
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.media3.common.AudioAttributes
import androidx.media3.common.C
import androidx.media3.common.MediaItem
import androidx.media3.common.MediaMetadata
import androidx.media3.common.Player
import androidx.media3.common.util.UnstableApi
import androidx.media3.datasource.DefaultDataSource
import androidx.media3.datasource.okhttp.OkHttpDataSource
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.exoplayer.source.DefaultMediaSourceFactory
import androidx.media3.session.MediaController
import androidx.media3.session.MediaSession
import androidx.media3.session.MediaSessionService
import androidx.media3.session.SessionToken
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.guava.await
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import okhttp3.OkHttpClient
import org.json.JSONObject

/** Musikk og lydbøker på telefonen: spiller videre i bakgrunnen, på låseskjermen og over Bluetooth (MediaSession).
 *  Strømmer fra Olympus med enhetsnøkkelen, eller fra en nedlastet fil når den finnes. */
@OptIn(UnstableApi::class)
class PlayerService : MediaSessionService() {
    private var session: MediaSession? = null

    override fun onCreate() {
        super.onCreate()
        val store = Store(this)
        val remote = OkHttpDataSource.Factory(authedClient(store))
        val player = ExoPlayer.Builder(this)
            .setMediaSourceFactory(DefaultMediaSourceFactory(DefaultDataSource.Factory(this, remote)))
            .setAudioAttributes(AudioAttributes.Builder().setUsage(C.USAGE_MEDIA).setContentType(C.AUDIO_CONTENT_TYPE_MUSIC).build(), true)
            .setHandleAudioBecomingNoisy(true) // pause når hodetelefonene/Bluetooth kobles fra
            .build()
        session = MediaSession.Builder(this, player).setCallback(object : MediaSession.Callback {
            // Tjenesten må være synlig for systemet (låseskjerm, Bluetooth-knapper), men bare Zeus selv får bytte hva
            // som spilles. Andre apper får play/pause/neste/spoling – aldri legge inn egne adresser.
            override fun onConnect(session: MediaSession, controller: MediaSession.ControllerInfo): MediaSession.ConnectionResult {
                if (controller.packageName == packageName) return MediaSession.ConnectionResult.AcceptedResultBuilder(session).build()
                val cmds = Player.Commands.Builder().addAll(Player.COMMAND_PLAY_PAUSE, Player.COMMAND_SEEK_TO_NEXT,
                    Player.COMMAND_SEEK_TO_PREVIOUS, Player.COMMAND_SEEK_TO_NEXT_MEDIA_ITEM, Player.COMMAND_SEEK_TO_PREVIOUS_MEDIA_ITEM,
                    Player.COMMAND_SEEK_IN_CURRENT_MEDIA_ITEM, Player.COMMAND_STOP, Player.COMMAND_GET_CURRENT_MEDIA_ITEM,
                    Player.COMMAND_GET_METADATA, Player.COMMAND_GET_TIMELINE).build()
                return MediaSession.ConnectionResult.AcceptedResultBuilder(session).setAvailablePlayerCommands(cmds).build()
            }
        }).build()
    }

    override fun onGetSession(controllerInfo: MediaSession.ControllerInfo) = session

    override fun onTaskRemoved(rootIntent: android.content.Intent?) {
        val p = session?.player
        if (p == null || !p.playWhenReady || p.mediaItemCount == 0) stopSelf()
    }

    override fun onDestroy() {
        session?.run { player.release(); release() }
        session = null
        super.onDestroy()
    }
}

/** HTTP-klient som legger på enhetsnøkkelen bare for Olympus selv (samme vert og port som paringen) – aldri for
 *  andre adresser, så nøkkelen ikke kan lekke om noen klarer å gi spilleren en fremmed URL. */
fun authedClient(store: Store): OkHttpClient = OkHttpClient.Builder().addInterceptor { chain ->
    val req = chain.request()
    val hostPort = "${req.url.host}:${req.url.port}"
    if (store.host.isNotEmpty() && (hostPort == store.host || (req.url.port == 80 && req.url.host == store.host)))
        chain.proceed(req.newBuilder().header("Authorization", "Bearer ${store.token}").build())
    else chain.proceed(req)
}.build()

/** Appens side av spilleren: kø, nå spilles, posisjon – og lagring av hvor du er i lydbøker. */
class Playback(private val context: Context, private val hub: Hub) {
    var controller by mutableStateOf<MediaController?>(null)
    var title by mutableStateOf("")
    var artist by mutableStateOf("")
    var coverPath by mutableStateOf<String?>(null)
    var playing by mutableStateOf(false)
    var position by mutableStateOf(0L)
    var duration by mutableStateOf(0L)
    var bookId by mutableStateOf<String?>(null)
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main)

    fun connect() {
        if (controller != null) return
        scope.launch {
            val token = SessionToken(context, ComponentName(context, PlayerService::class.java))
            val c = runCatching { MediaController.Builder(context, token).buildAsync().await() }.getOrNull() ?: return@launch
            controller = c
            c.addListener(object : Player.Listener {
                override fun onEvents(player: Player, events: Player.Events) = sync()
            })
            sync()
            while (isActive) {
                position = c.currentPosition
                duration = c.duration.coerceAtLeast(0)
                if (c.isPlaying && bookId != null) saveBookProgress()
                trackListening(c)
                delay(1000)
            }
        }
    }

    private var lastSave = 0L
    private var listenId: String? = null
    private var listened = 0.0
    private var listenDur = 0.0

    /** Musikksmaken: hvor lenge hver sang faktisk ble hørt; meldes til Olympus når sangen bytter (hoppet over = kort). */
    private fun trackListening(c: MediaController) {
        val id = c.currentMediaItem?.mediaId?.takeIf { bookId == null && !it.contains('-') }
        if (id != listenId) {
            listenId?.let { prev -> if (listened >= 1) report(prev, listened, listenDur) }
            listenId = id; listened = 0.0
        }
        if (id != null && c.isPlaying) { listened += 1.0; listenDur = c.duration.coerceAtLeast(0) / 1000.0 }
    }

    private fun report(id: String, sec: Double, dur: Double) {
        scope.launch { runCatching { hub.api.post("/media/played", JSONObject().put("id", id).put("listened", sec).put("duration", dur).put("source", "telefon")) } }
    }

    val currentId: String? get() = controller?.currentMediaItem?.mediaId
    fun queueIds(fromCurrent: Boolean = true): List<String> {
        val c = controller ?: return emptyList()
        val all = (0 until c.mediaItemCount).map { c.getMediaItemAt(it).mediaId }.filter { !it.contains('-') }
        return if (fromCurrent) all.drop(c.currentMediaItemIndex.coerceAtLeast(0)) else all
    }

    private fun saveBookProgress() {
        val id = bookId ?: return
        val now = System.currentTimeMillis()
        if (now - lastSave < 15_000) return
        lastSave = now
        val c = controller ?: return
        val absolute = (c.currentMediaItem?.mediaMetadata?.extras?.getDouble("start") ?: 0.0) + c.currentPosition / 1000.0
        scope.launch { runCatching { hub.api.put("/media/progress/$id", JSONObject().put("position", absolute)) } }
    }

    private fun sync() {
        val c = controller ?: return
        val md = c.mediaMetadata
        title = md.title?.toString().orEmpty()
        artist = md.artist?.toString().orEmpty()
        coverPath = md.extras?.getString("cover")
        playing = c.isPlaying
    }

    private fun item(id: String, path: String, title: String, artist: String, cover: String?, start: Double = 0.0): MediaItem {
        val local = Offline.file(context, id)
        val uri = if (local.exists() && local.length() > 0) Uri.fromFile(local) else Uri.parse("http://${hub.store.host}/api/v1$path")
        val extras = Bundle().apply { putString("cover", cover); putDouble("start", start) }
        return MediaItem.Builder().setMediaId(id).setUri(uri)
            .setMediaMetadata(MediaMetadata.Builder().setTitle(title).setArtist(artist).setExtras(extras).build()).build()
    }

    fun playTracks(tracks: List<Track>, startIndex: Int = 0, positionMs: Long = 0) {
        val c = controller ?: return
        bookId = null
        c.setMediaItems(tracks.map { t -> item(t.id, "/media/track/${t.id}/stream", t.title, t.artist,
            if (t.cover) "/media/track/${t.id}/cover" else null) }, startIndex.coerceIn(0, (tracks.size - 1).coerceAtLeast(0)), positionMs)
        c.prepare(); c.play()
    }

    fun playBook(book: JSONObject) {
        val c = controller ?: return
        val id = book.getString("id")
        val parts = book.optJSONArray("parts").list { it }
        if (parts.isEmpty()) return
        val pos = book.optDouble("position", 0.0)
        val items = parts.map { p ->
            item("$id-${p.optInt("idx")}", "/media/book/$id/part/${p.optInt("idx")}", p.optString("title"), book.optString("title"), null,
                p.optDouble("start", 0.0))
        }
        // Finn delen og stedet du var kommet til.
        val sameFile = parts.map { it.optString("start") }.distinct().size > 1 && items.map { it.localConfiguration?.uri }.distinct().size == 1
        bookId = id
        if (sameFile) {
            // Én fil med kapitler (m4b): spill fila og hopp til posisjonen.
            c.setMediaItems(listOf(items.first()), 0, (pos * 1000).toLong())
        } else {
            var idx = parts.indexOfLast { it.optDouble("start", 0.0) <= pos }.coerceAtLeast(0)
            if (idx >= items.size) idx = 0
            c.setMediaItems(items, idx, ((pos - parts[idx].optDouble("start", 0.0)) * 1000).toLong().coerceAtLeast(0))
        }
        c.prepare(); c.play()
    }

    fun toggle() { controller?.let { if (it.isPlaying) it.pause() else it.play() } }
    fun next() { controller?.seekToNextMediaItem() }
    fun prev() { controller?.seekToPreviousMediaItem() }
    fun seek(ms: Long) { controller?.seekTo(ms) }
    fun stop() { controller?.let { it.stop(); it.clearMediaItems() }; bookId = null }
}
