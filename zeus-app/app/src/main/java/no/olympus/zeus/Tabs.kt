package no.olympus.zeus

import androidx.compose.foundation.layout.displayCutoutPadding
import androidx.compose.ui.layout.onSizeChanged
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.foundation.gestures.detectTapGestures
import android.content.Context
import androidx.compose.ui.unit.sp
import androidx.compose.runtime.key
import androidx.annotation.OptIn
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.items
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.media3.common.MediaItem
import androidx.media3.common.MimeTypes
import androidx.media3.common.util.UnstableApi
import androidx.media3.datasource.DefaultDataSource
import androidx.media3.datasource.okhttp.OkHttpDataSource
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.exoplayer.source.DefaultMediaSourceFactory
import androidx.media3.ui.PlayerView
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import okhttp3.OkHttpClient
import org.json.JSONObject

/** Hent JSON fra Olympus inn i en Compose-tilstand (og hent på nytt når `key` endres). */
@Composable
fun rememberJson(hub: Hub, path: String, key: Any? = null): JSONObject? {
    var data by remember(path) { mutableStateOf<JSONObject?>(null) }
    LaunchedEffect(path, key, hub.mediaVersion) { data = runCatching { hub.api.get(path) }.getOrNull() }
    return data
}

// --- musikk -------------------------------------------------------------------------------------
@Composable
fun Empty(text: String) = T(text, Modifier.padding(20.dp), color = Z.muted)

/** Tom liste: et dempet motiv over forklaringen, i hele bredden. */
@Composable
fun EmptyArt(art: String, text: String) {
    Column(Modifier.fillMaxWidth().padding(vertical = 30.dp, horizontal = 20.dp), horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.spacedBy(16.dp)) {
        Art(art, color = Z.dim, size = 9f, glow = false)
        T(text, color = Z.muted, align = androidx.compose.ui.text.style.TextAlign.Center)
    }
}

@Composable
fun TrackRow(t: Track, current: Boolean, onPlay: () -> Unit, onAdd: () -> Unit, onRemove: (() -> Unit)? = null) {
    Row(Modifier.fillMaxWidth().clickable(onClick = onPlay).padding(vertical = 6.dp), verticalAlignment = Alignment.CenterVertically) {
        T(if (current) "▶ " else "  ", color = Z.gold)
        Column(Modifier.weight(1f)) {
            T(t.title, color = if (current) Z.gold else Z.text, maxLines = 1)
            T("${t.artist} · ${clock(t.duration)}", color = Z.muted, size = 11, maxLines = 1)
        }
        if (onRemove != null) Box(Modifier.clickable(onClick = onRemove).padding(10.dp)) { T("−", color = Z.muted) }
        Box(Modifier.clickable(onClick = onAdd).padding(10.dp)) { T("+≡", color = Z.gold, size = 12) }
    }
}

@Composable
fun TrackList(hub: Hub, play: Playback, title: String, path: String, onBack: () -> Unit, onAdd: (Track) -> Unit, playlistId: String? = null) {
    val scope = rememberCoroutineScope()
    val tracks = rememberJson(hub, path)?.optJSONArray("items").list(Track::from)
    LazyColumn(Modifier.padding(horizontal = 16.dp)) {
        item {
            Box(Modifier.clickable(onClick = onBack).padding(vertical = 8.dp)) { T(t("‹ tilbake"), color = Z.muted) }
            T(title, color = Z.gold, bold = true, glow = true)
            Row(Modifier.padding(vertical = 8.dp), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Btn(t("[ ▶ spill ]"), Modifier.weight(1f), solid = true) { play.playTracks(tracks) }
                Btn(t("[ ⤨ bland ]"), Modifier.weight(1f)) { play.playTracks(tracks.shuffled()) }
                Btn(t("[ PC ]"), Modifier.weight(0.7f)) {
                    scope.launch { runCatching { hub.api.post("/pc/play", JSONObject().put("tracks", org.json.JSONArray(tracks.map { it.id }))) } }
                    hub.toast = t("Spiller på PC-en")
                }
            }
            if (playlistId != null) Btn(t("[ slett spillelisten ]"), Modifier.fillMaxWidth(), danger = true) {
                scope.launch { runCatching { hub.api.delete("/media/playlist/$playlistId") }; hub.mediaVersion++; onBack() }
            }
        }
        items(tracks, key = { it.id }) { t ->
            TrackRow(t, play.title == t.title, { play.playTracks(tracks, tracks.indexOf(t)) }, { onAdd(t) },
                if (playlistId != null) ({
                    scope.launch {
                        runCatching { hub.api.patch("/media/playlist/$playlistId", JSONObject().put("tracks", org.json.JSONArray(tracks.filter { it.id != t.id }.map { it.id }))) }
                        hub.mediaVersion++
                    }
                }) else null)
        }
    }
}

@Composable
fun AddToPlaylist(hub: Hub, t: Track, onClose: () -> Unit) {
    val scope = rememberCoroutineScope()
    val lists = rememberJson(hub, "/media/playlists")?.optJSONArray("items").list(Playlist::from)
    var name by remember { mutableStateOf("") }
    androidx.compose.ui.window.Dialog(onDismissRequest = onClose) {
        Column(Modifier.fillMaxWidth().background(Z.bg).border(1.dp, Z.gold).padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            T(t("≡ LEGG «%s» I", t.title), color = Z.gold, bold = true, maxLines = 1)
            lists.forEach { p ->
                Box(Modifier.fillMaxWidth().border(1.dp, Z.line).clickable {
                    scope.launch { runCatching { hub.api.patch("/media/playlist/${p.id}", JSONObject().put("add", org.json.JSONArray(listOf(t.id)))) } }
                    hub.mediaVersion++; hub.toast = t("La til i %s", p.name); onClose()
                }.padding(10.dp)) { T(t("≡ %s  (%s)", p.name, p.tracks)) }
            }
            Field(name, { name = it }, t("ny spilleliste …"), Modifier.fillMaxWidth())
            Btn(t("[ lag og legg til ]"), Modifier.fillMaxWidth(), gold = true) {
                scope.launch {
                    runCatching { hub.api.post("/media/playlists", JSONObject().put("name", name.ifBlank { t("Ny spilleliste") }).put("tracks", org.json.JSONArray(listOf(t.id)))) }
                    hub.mediaVersion++; hub.toast = t("Laget «%s»", name.ifBlank { "Ny spilleliste" }); onClose()
                }
            }
        }
    }
}

// --- film ---------------------------------------------------------------------------------------
@OptIn(androidx.compose.foundation.ExperimentalFoundationApi::class)
@Composable
fun FilmTab(hub: Hub, onPlay: (Video) -> Unit, onEdit: (String) -> Unit = {}) {
    val data = rememberJson(hub, "/media/videos")
    val films = data?.optJSONArray("films").list(Video::from)
    val shows = data?.optJSONArray("shows").list { s -> s.optString("name") to s.optJSONArray("episodes").list(Video::from) }
    var show by remember { mutableStateOf<Pair<String, List<Video>>?>(null) }
    var detail by remember { mutableStateOf<Video?>(null) }
    val ctx = LocalContext.current
    when {
        detail != null -> VideoDetail(hub, detail!!, { detail = null }, onPlay, ctx)
        show != null -> LazyColumn(Modifier.padding(horizontal = 16.dp)) {
            item { Box(Modifier.clickable { show = null }.padding(vertical = 8.dp)) { T(t("‹ serier"), color = Z.muted) }; T(show!!.first, color = Z.gold, bold = true, glow = true) }
            items(show!!.second, key = { it.id }) { e ->
                Row(Modifier.fillMaxWidth().clickable { detail = e }.padding(vertical = 8.dp), verticalAlignment = Alignment.CenterVertically) {
                    T(e.label, Modifier.weight(1f), maxLines = 1)
                    if (e.position > 5) T(bar(e.position / e.duration.coerceAtLeast(1.0), 6), color = Z.gold, size = 10)
                }
            }
        }
        else -> LazyVerticalGrid(GridCells.Fixed(3), Modifier.padding(horizontal = 16.dp), horizontalArrangement = Arrangement.spacedBy(8.dp),
            verticalArrangement = Arrangement.spacedBy(10.dp)) {
            if (films.isEmpty() && shows.isEmpty()) item(span = { androidx.compose.foundation.lazy.grid.GridItemSpan(maxLineSpan) }) {
                EmptyArt(Ascii.poster("tom").first, t("Ingen filmer ennå. Legg filer i ~/Filmer og ~/Serier på PC-en – eller be Zeus: «finn Metropolis fra 1927»."))
            }
            items(films, key = { it.id }) { f ->
                Column(Modifier.combinedClickable(onClick = { detail = f }, onLongClick = { onEdit(f.id) })) {
                    RemoteImage(hub.store, "/media/video/${f.id}/poster", Modifier.fillMaxWidth().aspectRatio(2f / 3f).border(1.dp, Z.line), "▶",
                        fallback = { Ascii.poster(f.id).let { (art, col) -> Art(art, color = col, size = 7f, glow = false) } })
                    T(f.title, size = 11, maxLines = 1)
                    T(if (f.year > 0) f.year.toString() else "", color = Z.muted, size = 10)
                }
            }
            items(shows, key = { it.first }) { s ->
                Column(Modifier.clickable { show = s }) {
                    RemoteImage(hub.store, "/media/video/${s.second.first().id}/poster", Modifier.fillMaxWidth().aspectRatio(2f / 3f).border(1.dp, Z.line), "▶",
                        fallback = { Ascii.poster(s.first).let { (art, col) -> Art(art, color = col, size = 7f, glow = false) } })
                    T(s.first, size = 11, maxLines = 1)
                    T("${s.second.size} episoder", color = Z.muted, size = 10)
                }
            }
        }
    }
}

@Composable
fun VideoDetail(hub: Hub, v: Video, onBack: () -> Unit, onPlay: (Video) -> Unit, ctx: Context) {
    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(horizontal = 16.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
        Box(Modifier.clickable(onClick = onBack).padding(vertical = 8.dp)) { T(t("‹ tilbake"), color = Z.muted) }
        RemoteImage(hub.store, "/media/video/${v.id}/poster", Modifier.fillMaxWidth().aspectRatio(16f / 9f).border(1.dp, Z.line), "▶",
            fallback = { Ascii.poster(v.id).let { (art, col) -> Art(art, color = col, size = 10f) } })
        T(if (v.kind == "episode") "${v.show} · ${v.label}" else v.title, color = Z.gold, bold = true, glow = true)
        T(listOfNotNull(v.year.takeIf { it > 0 }?.toString(), clock(v.duration), if (v.direct) t("spilles direkte") else t("omkodes på PC-en")).joinToString(" · "), color = Z.muted, size = 12)
        if (v.position > 5) T(t("kommet til %s  %s", clock(v.position), bar(v.position / v.duration.coerceAtLeast(1.0), 12)), color = Z.gold, size = 12)
        // Filminfo fra TMDb (når nøkkelen er lagt inn): sjanger, vurdering, handling, skuespillere
        val info = rememberJson(hub, "/media/video/${v.id}")?.optJSONObject("info")
        if (info != null) {
            val genres = info.optJSONArray("genres")?.let { a -> (0 until a.length()).map { a.getString(it) } }.orEmpty()
            val rating = info.optDouble("rating", 0.0)
            T(listOfNotNull(genres.joinToString(" · ").ifEmpty { null }, if (rating > 0) "★ %.1f".format(rating) else null)
                .joinToString("   "), color = Z.muted, size = 12)
            info.optString("overview").takeIf { it.isNotBlank() }?.let { T(it, size = 13) }
            info.optJSONArray("cast")?.let { a -> (0 until a.length()).map { a.getString(it) } }?.takeIf { it.isNotEmpty() }
                ?.let { T(t("Med: ") + it.joinToString(", "), color = Z.muted, size = 11) }
        }
        Btn(if (v.position > 5) t("[ ▶ fortsett ]") else t("[ ▶ spill ]"), Modifier.fillMaxWidth(), solid = true) { onPlay(v) }
        val local = Offline.has(ctx, v.id)
        if (v.direct) Btn(if (local) t("[ ✓ lastet ned · slett ]") else t("[ ↓ last ned til telefonen ]"), Modifier.fillMaxWidth()) {
            if (local) Offline.delete(ctx, v.id) else { Offline.download(ctx, hub.store, "/media/video/${v.id}/file", v.id, v.title); hub.toast = t("Laster ned «%s»", v.title) }
        }
        TvCast(hub, v)
        val scope = rememberCoroutineScope()
        var fetching by remember { mutableStateOf(false) }
        Box(Modifier.fillMaxWidth().clickable(enabled = !fetching) {
            fetching = true
            scope.launch {
                runCatching { hub.api.post("/media/video/${v.id}/subtitles/fetch", JSONObject().put("lang", "no")) }
                    .onSuccess { hub.toast = t("Lagret %s", it.optString("saved")); hub.mediaVersion++ }
                    .onFailure { hub.toast = it.message ?: t("Fant ingen undertekst") }
                fetching = false
            }
        }.padding(vertical = 6.dp)) { T(if (fetching) t("henter undertekst …") else t("↓ hent norsk undertekst (OpenSubtitles)"), color = Z.muted, size = 11) }
    }
}

/** «▭ spill på TV»: én knapp hjemme og borte. TV-er PC-en ser (hjemmenettet) styres av PC-en; TV-er telefonen ser
 *  (nettet du står på, f.eks. hos en venn) får filmen via telefonen. Samme TV sett av begge = hjemme (PC-en). */
@Composable
fun TvCast(hub: Hub, v: Video) {
    val scope = rememberCoroutineScope()
    val ctx = LocalContext.current
    var picking by remember { mutableStateOf(false) }
    var pcTvs by remember { mutableStateOf<List<JSONObject>?>(null) }
    var phoneTvs by remember { mutableStateOf<List<PhoneTv>?>(null) }
    var now by remember { mutableStateOf<JSONObject?>(null) }
    var viaPhone by remember { mutableStateOf(PhoneCast.now != null) }
    var busy by remember { mutableStateOf(false) }
    var tick by remember { mutableStateOf(0) }
    suspend fun refresh(force: Boolean = false) {
        runCatching { hub.api.get("/tv" + if (force) "?refresh=true" else "") }.onSuccess {
            pcTvs = it.optJSONArray("devices").list { d -> d }; now = it.optJSONObject("now")
        }.onFailure { pcTvs = emptyList() }
    }
    LaunchedEffect(v.id) { refresh() }
    LaunchedEffect(now?.optBoolean("playing"), viaPhone) {  // posisjonen oppdateres mens filmen går på TV-en
        while ((now?.optBoolean("playing") == true && now?.optString("video") == v.id) || viaPhone) {
            kotlinx.coroutines.delay(5000); if (viaPhone) tick++ else refresh()
        }
    }
    fun control(action: String, position: Double? = null) = scope.launch {
        busy = true
        if (viaPhone) runCatching { PhoneCast.control(ctx, hub, action, position ?: 0.0) }
            .onSuccess { if (action == "stop") viaPhone = false }.onFailure { hub.toast = t("TV-en svarte ikke: %s", it.message) }
        else runCatching { hub.api.post("/tv/control", JSONObject().put("action", action).apply { position?.let { put("position", it) } }) }
            .onSuccess { now = it }.onFailure { hub.toast = t("TV-en svarte ikke: %s", it.message) }
        busy = false
    }
    val herePc = now?.optBoolean("playing") == true && now?.optString("video") == v.id
    if (herePc || viaPhone) {
        val n = now
        val name = if (viaPhone) PhoneCast.now?.name.orEmpty() else n?.optString("device_name").orEmpty()
        val pos = if (viaPhone) { tick; PhoneCast.position(ctx) } else n?.optDouble("position", n.optDouble("start", 0.0))
        val paused = if (viaPhone) { tick; PhoneCast.paused(ctx) } else n?.optBoolean("paused") == true
        Column(Modifier.fillMaxWidth().border(1.dp, Z.gold).padding(10.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            T(t("▭ på %s", name) + if (viaPhone) t(" · via telefonen") else if (n?.optString("mode") == "transcode") t(" · omkodes på PC-en") else "", color = Z.gold, size = 12)
            if (pos != null) T("${clock(pos)} / ${clock(v.duration)}", color = Z.muted, size = 11)
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                val p = pos ?: 0.0
                Btn("−30", Modifier.weight(1f)) { control("seek", (p - 30).coerceAtLeast(0.0)) }
                if (paused) Btn("▶", Modifier.weight(1f), gold = true) { control("resume") }
                else Btn("⏸", Modifier.weight(1f), gold = true) { control("pause") }
                Btn("+30", Modifier.weight(1f)) { control("seek", p + 30) }
                Btn("■", Modifier.weight(1f), danger = true) { control("stop") }
            }
        }
        return
    }
    Btn(if (busy) t("[ starter på TV-en … ]") else t("[ ▭ spill på TV ]"), Modifier.fillMaxWidth()) {
        if (!busy) { picking = true; pcTvs = null; phoneTvs = null
            scope.launch { refresh(true) }
            scope.launch { phoneTvs = runCatching { PhoneCast.discover(ctx) }.getOrDefault(emptyList()) } }
    }
    if (picking) androidx.compose.ui.window.Dialog(onDismissRequest = { picking = false }) {
        Column(Modifier.fillMaxWidth().background(Z.bg).border(1.dp, Z.gold).padding(16.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
            T(t("▭ HVILKEN TV?"), color = Z.gold, bold = true, glow = true)
            val pcs = pcTvs.orEmpty()
            val pcNames = pcs.map { it.optString("name") }.toSet()
            val phones = phoneTvs.orEmpty().filter { it.name !in pcNames }  // samme TV sett av begge: du er hjemme
            fun pick(label: String, start: suspend () -> Unit) {
                picking = false; busy = true
                scope.launch {
                    runCatching { start() }.onSuccess { hub.toast = t("Spiller på %s", label) }
                        .onFailure { hub.toast = t("Fikk ikke startet på TV-en: %s", it.message) }
                    busy = false
                }
            }
            pcs.forEach { d ->
                val twin = phoneTvs.orEmpty().firstOrNull { it.name == d.optString("name") }
                Row(Modifier.fillMaxWidth().pointerInput(d.optString("id"), twin) {
                    detectTapGestures(
                        onTap = { pick(d.optString("name")) { now = hub.api.post("/tv/play", JSONObject().put("video", v.id).put("device", d.optString("id"))); viaPhone = false } },
                        // langt trykk: send via telefonen (slik det virker borte) – for å prøve det hjemme
                        onLongPress = { twin?.let { t -> pick("${t.name} via telefonen") { PhoneCast.play(ctx, hub, t, v, v.position); viaPhone = true } } })
                }.padding(vertical = 10.dp), verticalAlignment = Alignment.CenterVertically) {
                    T("▭ ", color = Z.gold)
                    Column(Modifier.weight(1f)) { T(d.optString("name")); T("${d.optString("type")} · hjemme", color = Z.muted, size = 11) }
                }
            }
            phones.forEach { t ->
                Row(Modifier.fillMaxWidth().clickable {
                    pick(t.name) { PhoneCast.play(ctx, hub, t, v, v.position); viaPhone = true }
                }.padding(vertical = 10.dp), verticalAlignment = Alignment.CenterVertically) {
                    T("▭ ", color = Z.gold)
                    Column(Modifier.weight(1f)) {
                        T(t.name)
                        T("${if (t.kind == "chromecast") "Chromecast/Google TV" else "Smart-TV (DLNA)"} · her, via telefonen", color = Z.muted, size = 11)
                    }
                }
            }
            if (pcs.any { d -> phoneTvs.orEmpty().any { it.name == d.optString("name") } })
                T(t("hold inne en TV for å sende via telefonen (slik det virker borte)"), color = Z.dim, size = 10)
            if (pcTvs == null || phoneTvs == null) T(t("leter …"), color = Z.muted, size = 12)
            else if (pcs.isEmpty() && phones.isEmpty()) T(t("Fant ingen TV-er. Er TV-en på, og på samme nett som deg?"), color = Z.muted, size = 12)
            Box(Modifier.clickable { pcTvs = null; phoneTvs = null
                scope.launch { refresh(true) }; scope.launch { phoneTvs = runCatching { PhoneCast.discover(ctx) }.getOrDefault(emptyList()) }
            }.padding(vertical = 6.dp)) { T(t("↻ let igjen"), color = Z.muted, size = 11) }
        }
    }
}

@OptIn(UnstableApi::class)
@Composable
fun VideoPlayerScreen(hub: Hub, v: Video, onClose: () -> Unit) {
    val ctx = LocalContext.current
    val scope = rememberCoroutineScope()
    var detail by remember { mutableStateOf<JSONObject?>(null) }
    LaunchedEffect(v.id) { detail = runCatching { hub.api.get("/media/video/${v.id}") }.getOrNull() }
    val subs = detail?.optJSONArray("subtitles").list { it }
    var sub by remember { mutableStateOf(-1) }
    var offset by remember { mutableStateOf(if (v.direct) 0.0 else v.position) } // omkodet strøm starter her
    val player = remember {
        val remote = OkHttpDataSource.Factory(authedClient(hub.store))
        ExoPlayer.Builder(ctx).setMediaSourceFactory(DefaultMediaSourceFactory(DefaultDataSource.Factory(ctx, remote))).build()
    }
    fun load(startSec: Double) {
        val base = "http://${hub.store.host}/api/v1/media/video/${v.id}"
        val local = Offline.file(ctx, v.id)
        val uri = when {
            local.exists() && local.length() > 0 -> android.net.Uri.fromFile(local).toString()
            v.direct -> "$base/file"
            else -> "$base/transcode?start=%.1f&h=720".format(java.util.Locale.ROOT, startSec)
        }
        val subConf = subs.getOrNull(sub)?.let {
            MediaItem.SubtitleConfiguration.Builder(android.net.Uri.parse("$base/sub/${it.optInt("idx")}" + if (v.direct) "" else "?start=%.1f".format(java.util.Locale.ROOT, startSec)))
                .setMimeType(MimeTypes.TEXT_VTT).setLanguage(it.optString("lang")).setSelectionFlags(androidx.media3.common.C.SELECTION_FLAG_DEFAULT).build()
        }
        player.setMediaItem(MediaItem.Builder().setUri(uri).setSubtitleConfigurations(listOfNotNull(subConf)).build(),
            if (v.direct) (startSec * 1000).toLong() else 0L)
        player.prepare(); player.play()
    }
    LaunchedEffect(detail, sub) { if (detail != null) load(if (v.direct) maxOf(player.currentPosition / 1000.0, v.position) else offset + player.currentPosition / 1000.0) }
    LaunchedEffect(Unit) {
        while (true) {
            delay(10_000)
            val pos = offset + player.currentPosition / 1000.0
            runCatching { hub.api.put("/media/progress/${v.id}", JSONObject().put("position", if (v.direct) player.currentPosition / 1000.0 else pos).put("duration", v.duration)) }
        }
    }
    DisposableEffect(Unit) {
        onDispose {
            val pos = if (v.direct) player.currentPosition / 1000.0 else offset + player.currentPosition / 1000.0
            scope.launch { runCatching { hub.api.put("/media/progress/${v.id}", JSONObject().put("position", pos).put("duration", v.duration)) } }
            player.release()
        }
    }
    // Fullskjerm: skjul status- og navigasjonslinja mens filmen går (sveip fra kanten viser dem et øyeblikk).
    val view = androidx.compose.ui.platform.LocalView.current
    DisposableEffect(Unit) {
        val window = (ctx as? android.app.Activity)?.window
        val ctl = window?.let { androidx.core.view.WindowCompat.getInsetsController(it, view) }
        ctl?.systemBarsBehavior = androidx.core.view.WindowInsetsControllerCompat.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE
        ctl?.hide(androidx.core.view.WindowInsetsCompat.Type.systemBars())
        onDispose { ctl?.show(androidx.core.view.WindowInsetsCompat.Type.systemBars()) }
    }
    // Posisjon i hele filmen (omkodet strøm starter på «offset») og om den spiller – oppdateres jevnt.
    var pos by remember { mutableStateOf(v.position) }
    var playing by remember { mutableStateOf(true) }
    var controls by remember { mutableStateOf(true) }
    var lastTouch by remember { mutableStateOf(System.currentTimeMillis()) }
    var locked by remember { mutableStateOf(false) }   // lås: ingen berøring gjør noe før du holder inne ⚿
    val activity = ctx as? android.app.Activity
    var landscape by remember {
        mutableStateOf(ctx.resources.configuration.orientation == android.content.res.Configuration.ORIENTATION_LANDSCAPE)
    }
    androidx.activity.compose.BackHandler(enabled = locked) { }  // låst: heller ikke «tilbake» lukker filmen
    DisposableEffect(Unit) {  // tilbake til vanlig rotasjon når filmen lukkes
        onDispose { activity?.requestedOrientation = android.content.pm.ActivityInfo.SCREEN_ORIENTATION_UNSPECIFIED }
    }
    LaunchedEffect(Unit) {
        while (true) {
            delay(500)
            pos = if (v.direct) player.currentPosition / 1000.0 else offset + player.currentPosition / 1000.0
            playing = player.isPlaying
            if (controls && playing && System.currentTimeMillis() - lastTouch > 4000) controls = false
        }
    }
    fun seekTo(sec: Double) {
        val t = sec.coerceIn(0.0, (v.duration - 1).coerceAtLeast(0.0))
        if (v.direct) player.seekTo((t * 1000).toLong()) else { offset = t; load(t) }
        pos = t; lastTouch = System.currentTimeMillis()
    }
    val dark = androidx.compose.ui.graphics.Color.Black
    Box(Modifier.fillMaxSize().background(dark)
        .pointerInput(locked) { detectTapGestures { if (!locked) { controls = !controls; lastTouch = System.currentTimeMillis() } } }) {
        AndroidView({
            PlayerView(it).apply {
                this.player = player
                useController = false  // egne kontroller over bildet (samme stil som resten av appen)
                keepScreenOn = true
                resizeMode = androidx.media3.ui.AspectRatioFrameLayout.RESIZE_MODE_FIT
            }
        }, Modifier.fillMaxSize())
        if (locked) {
            // Låst: bare et lite ⚿ – hold inne for å låse opp (et vanlig trykk gjør ingenting)
            Box(Modifier.align(Alignment.TopEnd).displayCutoutPadding().padding(12.dp)
                .background(dark.copy(alpha = 0.45f))
                .pointerInput(Unit) { detectTapGestures(onLongPress = { locked = false; controls = true; lastTouch = System.currentTimeMillis() }) }
                .padding(horizontal = 12.dp, vertical = 8.dp)) { T(t("⚿ hold for å låse opp"), color = Z.muted, size = 11) }
        }
        if (controls && !locked) {
            val shade = dark.copy(alpha = 0.55f)
            Row(Modifier.align(Alignment.TopStart).fillMaxWidth().background(shade).displayCutoutPadding().padding(horizontal = 8.dp, vertical = 6.dp),
                verticalAlignment = Alignment.CenterVertically) {
                Box(Modifier.clickable(onClick = onClose).padding(8.dp)) { T(t("‹ lukk"), color = Z.muted) }
                T(v.label, Modifier.weight(1f), color = Z.gold, maxLines = 1)
                Box(Modifier.clickable {
                    landscape = !landscape
                    activity?.requestedOrientation = if (landscape) android.content.pm.ActivityInfo.SCREEN_ORIENTATION_SENSOR_LANDSCAPE
                        else android.content.pm.ActivityInfo.SCREEN_ORIENTATION_SENSOR_PORTRAIT
                    lastTouch = System.currentTimeMillis()
                }.padding(8.dp)) { T(t("↻ roter"), color = Z.muted) }
                Box(Modifier.clickable { locked = true; controls = false }.padding(8.dp)) { T(t("⚿ lås"), color = Z.muted) }
                if (subs.isNotEmpty()) Box(Modifier.clickable {
                    if (!v.direct) offset += player.currentPosition / 1000.0
                    sub = if (sub + 1 >= subs.size) -1 else sub + 1; lastTouch = System.currentTimeMillis()
                }.padding(8.dp)) { T(if (sub < 0) t("tekst av") else t("tekst: %s", subs[sub].optString("lang")), color = Z.muted) }
            }
            Row(Modifier.align(Alignment.Center), horizontalArrangement = Arrangement.spacedBy(28.dp), verticalAlignment = Alignment.CenterVertically) {
                Box(Modifier.clickable { seekTo(pos - 30) }.background(shade).padding(14.dp)) { T(t("−30s"), color = Z.text) }
                Box(Modifier.clickable {
                    if (player.isPlaying) player.pause() else player.play()
                    playing = !playing; lastTouch = System.currentTimeMillis()
                }.background(shade).padding(horizontal = 22.dp, vertical = 14.dp)) { T(if (playing) "❚❚" else "▶", color = Z.gold, size = 22, glow = true) }
                Box(Modifier.clickable { seekTo(pos + 30) }.background(shade).padding(14.dp)) { T(t("+30s"), color = Z.text) }
            }
            Column(Modifier.align(Alignment.BottomCenter).fillMaxWidth().background(shade).displayCutoutPadding()
                .padding(horizontal = 16.dp, vertical = 10.dp)) {
                // Tidslinja: trykk der du vil hoppe til.
                var barW by remember { mutableStateOf(1) }
                val frac = if (v.duration > 0) (pos / v.duration).toFloat().coerceIn(0f, 1f) else 0f
                Box(Modifier.fillMaxWidth().height(24.dp).onSizeChanged { barW = it.width.coerceAtLeast(1) }
                    .pointerInput(v.duration) { detectTapGestures { o -> seekTo(v.duration * (o.x / barW)) } },
                    contentAlignment = Alignment.CenterStart) {
                    Box(Modifier.fillMaxWidth().height(4.dp).background(Z.line))
                    Box(Modifier.fillMaxWidth(frac).height(4.dp).background(Z.gold))
                }
                Row {
                    T(clock(pos), color = Z.text, size = 12)
                    Spacer(Modifier.weight(1f))
                    T("−" + clock((v.duration - pos).coerceAtLeast(0.0)), color = Z.muted, size = 12)
                }
            }
        }
    }
}

// --- bøker --------------------------------------------------------------------------------------
@OptIn(androidx.compose.foundation.ExperimentalFoundationApi::class)
@Composable
fun BooksTab(hub: Hub, play: Playback, askZeus: (String) -> Unit, onEdit: (String) -> Unit = {}) {
    val books = rememberJson(hub, "/media/books")?.optJSONArray("items").list(Book::from)
    var reading by remember { mutableStateOf<Book?>(null) }
    val scope = rememberCoroutineScope()
    reading?.let { b -> Reader(hub, b, { reading = null }, askZeus); return }
    LazyColumn(Modifier.padding(horizontal = 16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        if (books.isEmpty()) item { EmptyArt(BOOK_ART, t("Ingen bøker ennå. E-bøker i ~/Bøker og lydbøker i ~/Lydbøker på PC-en – eller be Zeus: «finn Peer Gynt som lydbok».")) }
        else item { Section(if (books.any { it.position > 0 }) t("LESER NÅ") else t("BØKER"), Modifier.padding(top = 4.dp)) }
        items(books, key = { it.id }) { b ->
            Column(Modifier.fillMaxWidth().border(1.dp, Z.line).combinedClickable(onLongClick = { onEdit(b.id) }, onClick = {
                if (b.kind == "audio") scope.launch { runCatching { hub.api.get("/media/book/${b.id}") }.onSuccess { play.playBook(it) } }
                else reading = b
            }).padding(12.dp)) {
                Row { T(if (b.kind == "audio") "⏵ " else "§ ", color = Z.gold); T(b.title, Modifier.weight(1f), bold = true, maxLines = 1) }
                T(listOf(b.author, if (b.kind == "audio") t("lydbok · %s", clock(b.duration)) else t("e-bok · %s", b.format)).filter { it.isNotEmpty() }.joinToString(" · "),
                    color = Z.muted, size = 11)
                if (b.position > 0) T(if (b.kind == "audio") "${blocks(b.position / b.duration.coerceAtLeast(1.0), 14)} ${clock(b.duration - b.position)} igjen"
                    else t("kapittel %s", b.position.toInt() + 1), color = Z.gold, size = 11)
            }
        }
    }
}

@Composable
fun Reader(hub: Hub, b: Book, onClose: () -> Unit, askZeus: (String) -> Unit) {
    val text = rememberJson(hub, "/media/book/${b.id}/text")?.optJSONArray("items").list { it.optString("title") to it.optString("text") }
    var chapter by remember { mutableStateOf(b.position.toInt()) }
    var size by remember { mutableStateOf(hub.store.readerSize) }
    var toc by remember { mutableStateOf(false) }
    LaunchedEffect(chapter) { runCatching { hub.api.put("/media/progress/${b.id}", JSONObject().put("position", chapter.toDouble())) } }
    Column(Modifier.fillMaxSize()) {
        Row(Modifier.padding(horizontal = 8.dp, vertical = 4.dp), verticalAlignment = Alignment.CenterVertically) {
            Box(Modifier.clickable(onClick = onClose).padding(8.dp)) { T("‹", color = Z.muted, size = 16) }
            T(b.title, Modifier.weight(1f), color = Z.gold, maxLines = 1)
            // A− / A+: størrelsen huskes
            Box(Modifier.clickable { size = (size - 1).coerceAtLeast(13); hub.store.readerSize = size }.padding(8.dp)) { T(t("A−"), color = Z.muted, size = 12) }
            Box(Modifier.clickable { size = (size + 1).coerceAtMost(26); hub.store.readerSize = size }.padding(8.dp)) { T(t("A+"), color = Z.muted, size = 14) }
            Box(Modifier.clickable { toc = true }.padding(8.dp)) { T("≡", color = Z.gold, size = 16) }
            if (hub.ai) Box(Modifier.clickable { askZeus(t("Oppsummer kapittel %s av «%s» (bok-id %s).", chapter + 1, b.title, b.id)) }.padding(8.dp)) { T("ϟ", color = Z.gold, size = 15) }
        }
        if (text.isEmpty()) { Empty(t("henter teksten …")); return }
        val c = chapter.coerceIn(0, text.size - 1)
        if (toc) {  // kapittellista: trykk for å hoppe
            Column(Modifier.weight(1f).verticalScroll(rememberScrollState()).padding(horizontal = 16.dp)) {
                Section(t("KAPITLER"))
                text.forEachIndexed { i, (t, _) ->
                    Box(Modifier.fillMaxWidth().clickable { chapter = i; toc = false }.padding(vertical = 10.dp)) {
                        T("${i + 1}. $t", color = if (i == c) Z.gold else Z.text, maxLines = 2)
                    }
                }
            }
            return@Column
        }
        // Lesbar tekst: proporsjonal skrift med serifer, luft mellom linjer og avsnitt (før: monospace i én blokk).
        // Ny liste per kapittel, så neste kapittel starter øverst.
        val paras = remember(c, text) { text[c].second.split("\n\n").map { it.trim() }.filter { it.isNotEmpty() } }
        val style = androidx.compose.ui.text.TextStyle(fontFamily = androidx.compose.ui.text.font.FontFamily.Serif,
            fontSize = size.sp, lineHeight = (size * 1.55f).sp, color = Z.text)
        key(c) {
            LazyColumn(Modifier.weight(1f).padding(horizontal = 22.dp), contentPadding = androidx.compose.foundation.layout.PaddingValues(vertical = 12.dp)) {
                item { T(text[c].first, color = Z.gold, bold = true, size = size + 3); Spacer(Modifier.height(16.dp)) }
                items(paras) { p ->
                    val heading = p.length < 70 && p == p.uppercase() && p.any { it.isLetter() }
                    if (heading) T(p, Modifier.padding(top = 10.dp, bottom = 8.dp), color = Z.gold, bold = true, size = size - 1)
                    else androidx.compose.foundation.text.BasicText(p, Modifier.padding(bottom = (size * 0.8f).dp), style = style)
                }
                item {
                    if (c < text.size - 1) Btn(t("neste kapittel ›"), Modifier.fillMaxWidth().padding(vertical = 16.dp), gold = true) { chapter = c + 1 }
                }
            }
        }
        Row(Modifier.padding(horizontal = 12.dp, vertical = 6.dp), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Btn(t("‹ forrige"), Modifier.weight(1f)) { if (c > 0) chapter = c - 1 }
            T("${c + 1}/${text.size}", Modifier.align(Alignment.CenterVertically), color = Z.muted)
            Btn(t("neste ›"), Modifier.weight(1f), gold = true) { if (c < text.size - 1) chapter = c + 1 }
        }
    }
}


private const val BOOK_ART = """
   __________
  /         /|
 /  §      / |
/_________/  |
|  ≡≡≡≡≡  |  |
|  ≡≡≡≡≡  |  /
|  ≡≡≡    | /
|_________|/"""
