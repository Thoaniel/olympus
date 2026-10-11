package no.olympus.zeus

import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.layout.onSizeChanged
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import org.json.JSONObject
import java.util.Locale

private val NB = Locale.forLanguageTag("nb-NO")

/** Samme fem faner, i samme rekkefølge, som Lab's på PC-en (taster 1–5 der). */
val TABS = listOf("hjem" to "⌂", "zeus" to "ϟ", "plan" to "▦", "media" to "♪", "pc" to "◈")

@Composable
fun MainScreen(hub: Hub, play: Playback, startTab: String, effects: String, onEffects: (String) -> Unit, onUnpair: () -> Unit) {
    var tab by remember { mutableStateOf(if (TABS.any { it.first == startTab }) startTab else "hjem") }
    var video by remember { mutableStateOf<Video?>(null) }
    var player by remember { mutableStateOf(false) }
    var sheet by remember { mutableStateOf(false) }
    LaunchedEffect(Unit) { hub.update = Updater.check(hub) }
    LaunchedEffect(hub.navTo) { hub.navTo?.let { t -> if (TABS.any { it.first == t }) { tab = t; player = false }; hub.navTo = null } }
    LaunchedEffect(hub.sharedUrl) { if (hub.sharedUrl != null) { tab = "media"; player = false } }  // delt lenke → «+ legg til»
    video?.let { v -> VideoPlayerScreen(hub, v) { video = null; hub.mediaVersion++ }; return }
    Column(Modifier.fillMaxSize().statusBarsPadding().navigationBarsPadding().imePadding()) {
        // Rolig topplinje: navnet og én prikk. Alt annet (AI, modell, oppdatering, innstillinger) bak prikken.
        Row(Modifier.padding(start = 16.dp, end = 6.dp, top = 6.dp, bottom = 2.dp), verticalAlignment = Alignment.CenterVertically) {
            T(if (hub.ai) t("ϟ ZEUS") else "ZEUS", color = Z.gold, bold = true, glow = hub.ai)
            Spacer(Modifier.width(10.dp)); Rule(Modifier.weight(1f)); Spacer(Modifier.width(4.dp))
            StatusDot(hub.sync, hub.update != null) { sheet = true }
        }
        hub.toast?.let { msg ->
            Row(Modifier.padding(horizontal = 14.dp, vertical = 4.dp).fillMaxWidth().border(1.dp, Z.gold).padding(start = 10.dp),
                verticalAlignment = Alignment.CenterVertically) {
                T(msg, Modifier.weight(1f), size = 12)
                if (hub.undoSeq != null && tab == "plan") Box(Modifier.clickable { hub.undo() }.padding(10.dp)) { T(t("[ angre ]"), color = Z.gold, size = 12) }
                Box(Modifier.clickable { hub.toast = null }.padding(10.dp)) { T("✕", color = Z.muted) }
            }
            LaunchedEffect(msg) { delay(5000); if (hub.toast == msg) hub.toast = null }
        }
        Box(Modifier.weight(1f).fillMaxWidth()) {
            if (player) PlayerScreen(hub, play) { player = false }
            else when (tab) {
                "hjem" -> HomeTab(hub, play, { tab = it }, { player = true })
                "zeus" -> ZeusTab(hub)
                "plan" -> PlanTab(hub)
                "media" -> MediaTab(hub, play, { video = it }) { q -> hub.draft = q; tab = "zeus" }
                "pc" -> PcTab(hub, play) { player = true }
            }
        }
        if (!player && tab !in setOf("hjem", "pc")) NowPlayingBar(hub, play) { player = true }  // hjem/PC viser den allerede
        Column(Modifier.background(Z.surface)) {
            Rule(Modifier.padding(horizontal = 14.dp), fill = "═")
            Row(Modifier.fillMaxWidth()) {
                TABS.forEach { (name, icon) ->
                    val on = tab == name && !player
                    val label = if (name == "zeus" && !hub.ai) t("zeus av") else if (name == "pc") "PC" else t(name)
                    Column(Modifier.weight(1f).clickable { tab = name; player = false }.padding(top = 6.dp, bottom = 12.dp),
                        horizontalAlignment = Alignment.CenterHorizontally) {
                        T(icon, color = if (on) Z.gold else Z.muted, size = 13, glow = on)
                        T(if (on) "[${label.uppercase(NB)}]" else label, color = if (on) Z.gold else Z.muted, size = 10, bold = on, glow = on, maxLines = 1)
                    }
                }
            }
        }
    }
    if (sheet) StatusSheet(hub, effects, onEffects, onUnpair) { sheet = false }
}

/** Alt som før lå i topplinja: tilkobling, AI, modell, oppdatering og innstillinger – samlet ett sted. */
@Composable
fun StatusSheet(hub: Hub, effects: String, onEffects: (String) -> Unit, onUnpair: () -> Unit, onClose: () -> Unit) {
    val ctx = LocalContext.current
    val scope = rememberCoroutineScope()
    var confirmUnpair by remember { mutableStateOf(false) }
    androidx.compose.ui.window.Dialog(onDismissRequest = onClose) {
        Column(Modifier.fillMaxWidth().background(Z.bg).border(1.dp, Z.gold).padding(vertical = 12.dp).verticalScroll(rememberScrollState())) {
            Row(Modifier.padding(horizontal = 16.dp), verticalAlignment = Alignment.CenterVertically) {
                T(t("ϟ ZEUS"), color = Z.gold, bold = true, glow = true); Spacer(Modifier.weight(1f))
                Box(Modifier.clickable(onClick = onClose).padding(8.dp)) { T("✕", color = Z.muted) }
            }
            val (txt, col) = when (hub.sync) {
                Sync.OK -> "● koblet til ${hub.store.serverName.ifEmpty { "PC-en" }}" to Z.ok
                Sync.BUSY -> "⇅ synker …" to Z.gold
                Sync.OFFLINE -> "○ venter på PC-en – endringer sendes når den svarer" to Z.muted
            }
            T(txt, Modifier.padding(horizontal = 16.dp, vertical = 4.dp), color = col, size = 12)
            hub.update?.let { u ->
                var busy by remember { mutableStateOf(false) }
                ListRow(t("Zeus %s er klar på PC-en", u.optString("name")), icon = "⇪", subtitle = t("trykk for å oppdatere"), on = true) {
                    if (!busy) { busy = true; scope.launch { Updater.install(ctx, hub.store)?.let { hub.toast = it }; busy = false } }
                }
            }
            Group(t("AI")) {
                ListRow(if (hub.ai) t("AI er på") else t("AI er av"), icon = "ϟ", on = hub.ai,
                    subtitle = if (hub.ai) t("trykk for å slå av – frigjør GPU-en") else t("trykk for å slå på")) { hub.switchAi(!hub.ai) }
                ModelChip(hub)
            }
            Group(t("SKJERMEFFEKTER")) {
                Seg(listOf("av", "middels", "sterk"), effects) { onEffects(it) }
            }
            // Språk: følger telefonen, eller fast norsk/engelsk. Bytter med en gang (aktiviteten tegnes på nytt).
            val ctx = androidx.compose.ui.platform.LocalContext.current
            val langNow = hub.store.lang
            Group(t("SPRÅK")) {
                Seg(listOf("auto", "norsk", "english"), mapOf("auto" to "auto", "nb" to "norsk", "en" to "english")[langNow] ?: "auto") { v ->
                    hub.store.lang = mapOf("auto" to "auto", "norsk" to "nb", "english" to "en")[v] ?: "auto"
                    I18n.init(ctx)
                    (ctx as? android.app.Activity)?.recreate()
                }
            }
            Group(t("TELEFONEN")) {
                ListRow(if (confirmUnpair) t("Trykk igjen for å fjerne paringen") else t("Fjern paring"), icon = "✕", iconColor = Z.bad,
                    subtitle = t("telefonen må pares på nytt")) {
                    if (confirmUnpair) { onClose(); onUnpair() } else confirmUnpair = true
                }
                T(t("Zeus %s", BuildConfig.VERSION_NAME), Modifier.padding(horizontal = 16.dp, vertical = 6.dp), color = Z.dim, size = 11)
            }
        }
    }
}

// --- hjem -----------------------------------------------------------------------------------------
@Composable
fun HomeTab(hub: Hub, play: Playback, go: (String) -> Unit, openPlayer: () -> Unit) {
    val d = rememberJson(hub, "/dashboard", hub.planVersion)
    val today = java.time.LocalDate.now().toString()
    LazyColumn(Modifier.fillMaxSize()) {
        item {
            Group(t("▦ I DAG"), action = t("plan ›"), onAction = { go("plan") }) {
                val evs = hub.events.filter { it.date == today }
                if (evs.isEmpty()) T(t("ingenting i planen i dag"), Modifier.padding(horizontal = 16.dp, vertical = 6.dp), color = Z.muted)
                evs.forEach { ListRow(it.title, icon = if (it.source == "zeus") "ϟ" else "·", trailing = it.time) { go("plan") } }
                hub.events.firstOrNull { it.date > today }?.let { n ->
                    T(t("neste: %s.%s %s  %s", n.date.substring(8), n.date.substring(5, 7), n.time, n.title), Modifier.padding(horizontal = 16.dp, vertical = 2.dp),
                        color = Z.muted, size = 11, maxLines = 1)
                }
                d?.optJSONObject("weather")?.let { w ->
                    val temp = w.optDouble("temp").takeIf { !it.isNaN() }?.let { "%.0f°".format(it) } ?: "?"
                    T(t("☁ %s %s · %s", temp, weatherWord(w.optString("symbol")), w.optString("place")), Modifier.padding(horizontal = 16.dp, vertical = 2.dp),
                        color = Z.muted, size = 11, maxLines = 1)
                }
            }
        }
        item { Group(t("♪ SPILLES NÅ")) { NowPlayingCard(hub, play, openPlayer) } }
        val cont = d?.optJSONArray("continue").list { it }
        if (cont.isNotEmpty()) item {
            Group(t("↻ FORTSETT"), action = t("media ›"), onAction = { go("media") }) {
                cont.take(4).forEach { c ->
                    val frac = if (c.optDouble("duration", 0.0) > 0) c.optDouble("position") / c.optDouble("duration") else 0.0
                    ListRow(c.optString("title"), icon = if (c.optString("type") == "video") "▶" else "§", trailing = blocks(frac, 8)) { go("media") }
                }
            }
        }
        item {
            Group(t("◈ PC-EN"), action = t("PC ›"), onAction = { go("pc") }) {
                val s = d?.optJSONObject("server")
                if (s == null) T(if (d == null) t("henter …") else t("○ ingen kontakt"), Modifier.padding(horizontal = 16.dp, vertical = 6.dp), color = Z.muted)
                else ListRow(t("GPU %s · CPU %s · disk %s %", s.optDouble("gpu").takeIf { !it.isNaN() }?.let { "%.0f°".format(it) } ?: "?", s.optDouble("cpu").takeIf { !it.isNaN() }?.let { "%.0f°".format(it) } ?: "?", s.optInt("disk_pct")),
                    icon = "●", iconColor = Z.ok, subtitle = t("Zeus %s · backup %s", if (s.optBoolean("ai")) t("på") else t("av"), if (d.optJSONObject("backup")?.optBoolean("ok") == true) "✓" else "–")) { go("pc") }
            }
        }
        item { Spacer(Modifier.height(12.dp)) }
    }
}

// --- én felles avspiller ----------------------------------------------------------------------------
/** Hvem spiller: telefonen (har noe i spilleren) eller PC-en. Telefonen vinner hvis begge har noe. */
private fun source(hub: Hub, play: Playback): String? = when {
    play.title.isNotEmpty() -> "telefon"
    hub.pc?.optJSONObject("current") != null -> "pc"
    else -> null
}

@Composable
fun NowPlayingCard(hub: Hub, play: Playback, open: () -> Unit) {
    val src = source(hub, play)
    if (src == null) {
        ListRow(t("Ingenting spilles"), icon = "♪", iconColor = Z.dim, subtitle = t("velg noe i media eller spør Zeus"))
        return
    }
    val pcCur = hub.pc?.optJSONObject("current")
    val title = if (src == "telefon") play.title else pcCur?.optString("title").orEmpty()
    val artist = if (src == "telefon") play.artist else pcCur?.optString("artist").orEmpty()
    val playing = if (src == "telefon") play.playing else hub.pc?.optBoolean("playing") == true
    ListRow(title, on = true, subtitle = "$artist · ${if (src == "telefon") "på telefonen" else "på PC-en"}", onClick = open,
        leading = { EqBars(playing, Modifier.width(22.dp), bars = 4, height = 18.dp, gap = 2.dp) })
}

@Composable
fun NowPlayingBar(hub: Hub, play: Playback, open: () -> Unit) {
    val src = source(hub, play) ?: return
    val pcCur = hub.pc?.optJSONObject("current")
    val phone = src == "telefon"
    var tick by remember { mutableStateOf(0) }
    LaunchedEffect(src) { while (true) { delay(1000); tick++ } }
    val frac = if (phone) (if (play.duration > 0) play.position.toDouble() / play.duration else 0.0)
        else hub.pc?.optDouble("duration", 0.0)?.takeIf { it > 0 }?.let { (hub.pcPosition() + tick * 0) / it } ?: 0.0
    val playing = if (phone) play.playing else hub.pc?.optBoolean("playing") == true
    Column(Modifier.fillMaxWidth().background(Z.surface).clickable(onClick = open).padding(horizontal = 14.dp, vertical = 6.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            MiniEq(playing)
            Spacer(Modifier.width(10.dp))
            Column(Modifier.weight(1f)) {
                T(if (phone) play.title else pcCur?.optString("title").orEmpty(), color = Z.gold, maxLines = 1, glow = true)
                T((if (phone) play.artist else pcCur?.optString("artist").orEmpty()) + if (phone) t(" · telefonen") else t(" · PC-en"), color = Z.muted, size = 11, maxLines = 1)
            }
            Box(Modifier.clickable { if (phone) play.prev() else hub.pcControl("prev") }.padding(8.dp)) { T("⏮", color = Z.text) }
            Box(Modifier.clickable { if (phone) play.toggle() else hub.pcControl(if (playing) "pause" else "resume") }.padding(8.dp)) {
                T(if (playing) "❚❚" else "▶", color = Z.gold, size = 14)
            }
            Box(Modifier.clickable { if (phone) play.next() else hub.pcControl("next") }.padding(8.dp)) { T("⏭", color = Z.text) }
        }
        T(blocks(frac, 48), color = Z.gold, size = 7, maxLines = 1)
    }
}

/** Spilleren i full størrelse – for telefonen og PC-en, med flytting mellom dem. */
@Composable
fun PlayerScreen(hub: Hub, play: Playback, onClose: () -> Unit) {
    var adding by remember { mutableStateOf<Track?>(null) }
    var tick by remember { mutableStateOf(0) }
    LaunchedEffect(Unit) { hub.refreshPc(); while (true) { delay(1000); tick++ } }
    val src = source(hub, play)
    Column(Modifier.fillMaxSize()) {
        SubHeader(if (src == "pc") t("spilles på PC-en") else t("spilles på telefonen"), onClose)
        if (src == null) { EmptyArt(Ascii.LANDSCAPE, t("Ingenting spilles. Velg noe i media – eller be Zeus: «spill noe jeg liker».")); return }
        val phone = src == "telefon"
        val pcCur = hub.pc?.optJSONObject("current")
        val title = if (phone) play.title else pcCur?.optString("title").orEmpty()
        val artist = if (phone) play.artist else pcCur?.optString("artist").orEmpty()
        val playing = if (phone) play.playing else hub.pc?.optBoolean("playing") == true
        val pos = if (phone) play.position / 1000.0 else hub.pcPosition() + tick * 0
        val dur = if (phone) play.duration / 1000.0 else hub.pc?.optDouble("duration", 0.0) ?: 0.0
        val cover = if (phone) play.coverPath else pcCur?.optString("id")?.let { "/media/track/$it/cover" }
        val id = if (phone) play.currentId else pcCur?.optString("id")
        LazyColumn(Modifier.fillMaxSize().padding(horizontal = 18.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
            item {
                Box(Modifier.fillMaxWidth(), contentAlignment = Alignment.Center) {
                    RemoteImage(hub.store, cover, Modifier.size(200.dp).border(1.dp, Z.gold), fallback = { Art(Ascii.LANDSCAPE, size = 6f) })
                }
            }
            item {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Column(Modifier.weight(1f)) {
                        T(title, color = Z.gold, bold = true, glow = true, size = 15, maxLines = 2)
                        T(artist, color = Z.muted)
                    }
                    id?.let { LikeButtons(hub, it) }
                }
            }
            item { EqBars(playing, Modifier.fillMaxWidth()) }
            item {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    T(blocks(if (dur > 0) pos / dur else 0.0, 26), Modifier.weight(1f), color = Z.gold, size = 12, maxLines = 1)
                    T(t(" %s", clock(pos)), color = Z.muted, size = 12)
                }
            }
            item {
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    Btn("⏮", Modifier.weight(1f)) { if (phone) play.prev() else hub.pcControl("prev") }
                    Btn(if (playing) "❚❚" else "▶", Modifier.weight(1f), solid = true) {
                        if (phone) play.toggle() else hub.pcControl(if (playing) "pause" else "resume")
                    }
                    Btn("⏭", Modifier.weight(1f)) { if (phone) play.next() else hub.pcControl("next") }
                }
            }
            item {
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    if (phone) Btn(t("[ flytt til PC-en ]"), Modifier.weight(1f), gold = true) {
                        hub.moveToPc(play.queueIds(), play.position / 1000.0) { if (play.playing) play.toggle() }
                    } else Btn(t("[ flytt til telefonen ]"), Modifier.weight(1f), gold = true) {
                        hub.moveToPhone { tracks, idx, ms -> play.playTracks(tracks, idx, ms) }
                    }
                    if (phone) Btn(t("[+ i liste]")) {
                        play.controller?.currentMediaItem?.let { m ->
                            adding = Track(m.mediaId, m.mediaMetadata.title?.toString().orEmpty(), m.mediaMetadata.artist?.toString().orEmpty(), "", "", 0.0, false)
                        }
                    } else Btn(t("■ stopp"), danger = true) { hub.pcControl("stop") }
                }
            }
            if (phone) {
                val c = play.controller
                if (c != null) {
                    val q = (0 until c.mediaItemCount).map { c.getMediaItemAt(it) }
                    item { Section(t("KØ · %s spor", q.size), Modifier.padding(top = 6.dp)) }
                    items(q.size) { i ->
                        val cur = i == c.currentMediaItemIndex
                        ListRow(q[i].mediaMetadata.title?.toString().orEmpty(), icon = if (cur) "▶" else " ", on = cur, modifier = Modifier.padding(horizontal = 0.dp)) {
                            c.seekTo(i, 0); c.play()
                        }
                    }
                }
            } else {
                val q = hub.pc?.optJSONArray("upcoming").list { it }
                if (q.isNotEmpty()) {
                    item { Section(t("NESTE PÅ PC-EN"), Modifier.padding(top = 6.dp)) }
                    items(q) { t -> ListRow(t.optString("title"), icon = "·", subtitle = t.optString("artist")) }
                }
            }
        }
    }
    adding?.let { t -> AddToPlaylist(hub, t) { adding = null } }
}

/** ♥ og ✕: det Zeus lærer musikksmaken din av (sammen med hva du hører på og hopper over). */
@Composable
fun LikeButtons(hub: Hub, trackId: String) {
    val scope = rememberCoroutineScope()
    var value by remember(trackId) { mutableStateOf(0) }
    LaunchedEffect(trackId) { value = runCatching { hub.api.get("/media/like/$trackId").optInt("value") }.getOrDefault(0) }
    fun set(v: Int) {
        val nv = if (value == v) 0 else v
        value = nv
        scope.launch { runCatching { hub.api.put("/media/like/$trackId", JSONObject().put("value", nv)) } }
        if (nv != 0) hub.toast = if (nv > 0) t("♥ Zeus husker at du liker denne") else t("✕ Zeus spiller den sjeldnere")
    }
    Box(Modifier.clickable { set(1) }.padding(10.dp)) { T("♥", color = if (value > 0) Z.bad else Z.muted, size = 18, glow = value > 0) }
    Box(Modifier.clickable { set(-1) }.padding(10.dp)) { T("✕", color = if (value < 0) Z.gold else Z.muted, size = 16) }
}

// --- media: musikk · film · bøker --------------------------------------------------------------------
@Composable
fun MediaTab(hub: Hub, play: Playback, onVideo: (Video) -> Unit, askZeus: (String) -> Unit) {
    var part by remember { mutableStateOf("musikk") }
    var adding by remember { mutableStateOf<String?>(null) }   // url (tom = bare åpne dialogen)
    var showDownloads by remember { mutableStateOf(false) }
    var editing by remember { mutableStateOf<String?>(null) }
    val scope = rememberCoroutineScope()
    LaunchedEffect(hub.sharedUrl) { hub.sharedUrl?.let { adding = it; hub.sharedUrl = null } }
    LaunchedEffect(Unit) { hub.refreshDownloads() }
    if (showDownloads) { DownloadsView(hub) { showDownloads = false }; return }
    val kind = when (part) { "film" -> "film"; "bøker" -> "ebook"; else -> "music" }
    Column(Modifier.fillMaxSize()) {
        Seg(listOf("musikk", "film", t("bøker")), part) { part = it }
        // Det du kan be Zeus om, kan du også gjøre selv: legge til, se nedlastinger, skanne. Hold inne på noe for å endre det.
        Row(Modifier.fillMaxWidth().padding(horizontal = 8.dp), verticalAlignment = Alignment.CenterVertically) {
            Box(Modifier.clickable { adding = "" }.padding(8.dp)) { T(t("+ legg til"), color = Z.gold, size = 12, bold = true) }
            val active = hub.downloads.count { it.optString("status") in listOf(t("laster ned"), t("lastet ned")) }
            Box(Modifier.clickable { showDownloads = true }.padding(8.dp)) {
                T(t("↓ nedlastinger") + if (active > 0) t(" (%s)", active) else "", color = if (active > 0) Z.gold else Z.muted, size = 12)
            }
        }
        // Hintet om «hold inne» vises én gang (som melding), ikke som en fast linje over innholdet
        LaunchedEffect(Unit) {
            if (!hub.store.seenHint("longpress")) hub.toast = t("Tips: hold inne på et album, en film eller en bok for å redigere, bytte cover eller slette.")
        }
        Box(Modifier.weight(1f)) {
            when (part) {
                "musikk" -> MusicPane(hub, play) { editing = it }
                "film" -> FilmTab(hub, onVideo) { editing = it }
                else -> BooksTab(hub, play, askZeus) { editing = it }
            }
        }
    }
    adding?.let { u -> AddMediaDialog(hub, u, kind, { adding = null }) { adding = null; showDownloads = true } }
    editing?.let { id -> ItemSheet(hub, id) { editing = null } }
}

@Composable
fun MusicPane(hub: Hub, play: Playback, onEdit: (String) -> Unit) {
    var album by remember { mutableStateOf<Album?>(null) }
    var playlist by remember { mutableStateOf<Playlist?>(null) }
    var adding by remember { mutableStateOf<Track?>(null) }
    var query by remember { mutableStateOf("") }
    val scope = rememberCoroutineScope()
    when {
        album != null -> TrackList(hub, play, "${album!!.title} – ${album!!.artist}", "/media/album/${album!!.id}", { album = null }, { adding = it })
        playlist != null -> TrackList(hub, play, t("≡ %s", playlist!!.name), "/media/playlist/${playlist!!.id}", { playlist = null }, { adding = it },
            playlistId = playlist!!.id)
        else -> {
            val albums = rememberJson(hub, "/media/albums")?.optJSONArray("items").list(Album::from)
            val lists = rememberJson(hub, "/media/playlists")?.optJSONArray("items").list(Playlist::from)
            val found = if (query.length >= 2) rememberJson(hub, "/media/search?q=" + java.net.URLEncoder.encode(query, "UTF-8"))
                ?.optJSONArray("tracks").list(Track::from) else emptyList()
            LazyColumn(Modifier.fillMaxSize()) {
                item { Field(query, { query = it }, t("søk i musikken …"), Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 4.dp)) }
                if (query.length >= 2) {
                    item { Group(t("TREFF")) { if (found.isEmpty()) T(t("ingen treff"), Modifier.padding(horizontal = 16.dp), color = Z.muted) } }
                    items(found, key = { "s" + it.id }) { t ->
                        ListRow(t.title, icon = if (play.title == t.title) "▶" else "♪", subtitle = "${t.artist} · ${clock(t.duration)}",
                            on = play.title == t.title) { play.playTracks(found, found.indexOf(t)) }
                    }
                } else {
                    item {
                        Group(t("≡ SPILLELISTER"), action = t("+ ny"), onAction = {
                            scope.launch {
                                runCatching { hub.api.post("/media/playlists", JSONObject().put("name", t("Ny spilleliste"))) }
                                    .onSuccess { playlist = Playlist.from(it); hub.mediaVersion++ }
                            }
                        }) {
                            ListRow(t("Miks etter smaken din"), icon = "♥", iconColor = Z.bad, subtitle = t("det du liker + litt nytt fra artister du liker")) {
                                scope.launch {
                                    runCatching { hub.api.get("/media/mix?limit=40") }.onSuccess { r ->
                                        val mix = r.optJSONArray("items").list(Track::from)
                                        if (mix.isEmpty()) hub.toast = t("Ingen musikk å lage miks av ennå") else play.playTracks(mix)
                                    }.onFailure { hub.toast = t("Fikk ikke laget miksen") }
                                }
                            }
                            if (lists.isEmpty()) T(t("lag din første med «+ ny»"), Modifier.padding(horizontal = 16.dp), color = Z.dim, size = 11)
                            lists.forEach { p -> ListRow(p.name, icon = "≡", trailing = t("%s spor", p.tracks)) { playlist = p } }
                        }
                    }
                    item { Group(t("♪ ALBUM")) { if (albums.isEmpty()) EmptyArt(Ascii.VINYL, t("Ingen musikk ennå. Legg filer i ~/Musikk på PC-en, eller be Zeus finne noe.")) } }
                    items(albums, key = { it.id }) { a ->
                        ListRow(a.title, subtitle = a.artist, trailing = t("%s spor", a.tracks), onClick = { album = a }, onLongClick = { onEdit(a.id) }, leading = {
                            RemoteImage(hub.store, a.coverTrack?.let { "/media/track/$it/cover" }, Modifier.size(44.dp).border(1.dp, Z.line),
                                fallback = { Art(Ascii.VINYL, size = 2.6f, color = Z.gold.copy(alpha = 0.7f), glow = false) })
                        })
                    }
                }
                item { Spacer(Modifier.height(10.dp)) }
            }
        }
    }
    adding?.let { t -> AddToPlaylist(hub, t) { adding = null } }
}

// --- PC-en: avspilling, lyd, fjernkontroll, status, telefoner ------------------------------------------
@Composable
fun PcTab(hub: Hub, play: Playback, openPlayer: () -> Unit) {
    val scope = rememberCoroutineScope()
    var audio by remember { mutableStateOf<JSONObject?>(null) }
    var info by remember { mutableStateOf<JSONObject?>(null) }
    var devices by remember { mutableStateOf(emptyList<JSONObject>()) }
    var confirmSleep by remember { mutableStateOf(false) }
    suspend fun refresh() {
        audio = runCatching { hub.api.get("/audio") }.getOrNull()
        info = runCatching { hub.api.get("/pc/info") }.getOrNull()
        devices = runCatching { hub.api.get("/devices") }.getOrNull()?.optJSONArray("items").list { it }
    }
    LaunchedEffect(hub.mediaVersion) { refresh(); hub.refreshPc() }
    fun post(path: String, body: JSONObject) = scope.launch { runCatching { hub.api.post(path, body) }; refresh() }
    LazyColumn(Modifier.fillMaxSize()) {
        item {
            Group(t("♪ AVSPILLING PÅ PC-EN")) {
                val cur = hub.pc?.optJSONObject("current")
                if (cur == null) ListRow(t("Ingenting spilles på PC-en"), icon = "♪", iconColor = Z.dim,
                    subtitle = if (play.title.isNotEmpty()) t("trykk for å flytte «%s» hit", play.title) else t("velg noe i media, eller be Zeus"),
                    onClick = if (play.title.isNotEmpty()) ({ hub.moveToPc(play.queueIds(), play.position / 1000.0) { if (play.playing) play.toggle() } }) else null)
                else ListRow(cur.optString("title"), on = true, subtitle = cur.optString("artist"), onClick = openPlayer,
                    leading = { EqBars(hub.pc?.optBoolean("playing") == true, Modifier.width(22.dp), bars = 4, height = 18.dp, gap = 2.dp) })
            }
        }
        item {
            Group(t("◉ LYD")) {
                val outs = audio?.optJSONArray("outputs").list { it }
                val def = outs.firstOrNull { it.optBoolean("default") }
                if (def != null) {
                    val vol = def.optInt("volume")
                    val muted = def.optBoolean("muted")
                    // Én rad: demp (ikonet) · prosent · måler · − +
                    Row(Modifier.padding(horizontal = 16.dp, vertical = 4.dp), verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        Box(Modifier.clickable { post("/audio/mute", JSONObject().put("muted", !muted)) }.padding(6.dp)) {
                            T(if (muted) "✕" else "◉", color = if (muted) Z.bad else Z.gold, size = 16)
                        }
                        T(if (muted) "dempet" else "$vol %", Modifier.width(64.dp), color = if (muted) Z.bad else Z.text, maxLines = 1)
                        // Måleren fyller bredden; trykk på den for å sette volumet der du trykker.
                        var barW by remember { mutableStateOf(1) }
                        Box(Modifier.weight(1f).height(26.dp)
                            .onSizeChanged { barW = it.width.coerceAtLeast(1) }
                            .pointerInput(Unit) {
                                detectTapGestures { o -> post("/audio/volume", JSONObject().put("percent", ((o.x / barW) * 100).toInt().coerceIn(0, 100))) }
                            }, contentAlignment = Alignment.CenterStart) {
                            Box(Modifier.fillMaxWidth().height(10.dp).border(1.dp, Z.line))
                            Box(Modifier.fillMaxWidth((vol / 100f).coerceIn(0f, 1f)).height(10.dp).background(if (muted) Z.dim else Z.gold))
                        }
                        Btn("−") { post("/audio/volume", JSONObject().put("percent", (vol - 10).coerceAtLeast(0))) }
                        Btn("+") { post("/audio/volume", JSONObject().put("percent", (vol + 10).coerceAtMost(100))) }
                    }
                }
                // Bare utgangen som er i bruk; de andre bak «bytt utgang» (seks lydkort var bare støy)
                var pick by remember { mutableStateOf(false) }
                def?.let { ListRow(shortSink(it.optString("label")), icon = "●", on = true, trailing = if (pick) "lukk" else t("bytt ›")) { pick = !pick } }
                if (pick) outs.filter { !it.optBoolean("default") }.forEach { o ->
                    ListRow(shortSink(o.optString("label")), icon = "○", iconColor = Z.muted) {
                        pick = false; post("/audio/output", JSONObject().put("sink", o.optString("name")))
                    }
                }
                audio?.optJSONArray("bluetooth").list { it }.forEach { b ->
                    ListRow(b.optString("name"), icon = "ᛒ", subtitle = if (b.optBoolean("connected")) t("koblet til · trykk for å koble fra") else t("trykk for å koble til"),
                        on = b.optBoolean("connected")) {
                        post("/audio/bluetooth", JSONObject().put("mac", b.optString("mac")).put("connect", !b.optBoolean("connected")))
                    }
                }
            }
        }
        item {
            Group(t("⌁ FJERNKONTROLL")) {
                var screenOff by remember { mutableStateOf(false) }
                Column(Modifier.padding(horizontal = 16.dp, vertical = 6.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        Btn(t("⚿ lås"), Modifier.weight(1f)) { hub.pcAction("lock") }
                        Btn(if (screenOff) t("◑ skjerm på") else t("◐ skjerm av"), Modifier.weight(1f)) {
                            hub.pcAction(if (screenOff) "screen_on" else "screen_off"); screenOff = !screenOff
                        }
                    }
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        Btn(t("▲ Olympus"), Modifier.weight(1f)) { hub.pcAction("open_olympus") }
                        Btn(if (confirmSleep) t("trykk igjen") else t("☾ sov"), Modifier.weight(1f), danger = confirmSleep) {
                            if (confirmSleep) { confirmSleep = false; hub.pcAction("sleep", confirm = true) } else confirmSleep = true
                        }
                    }
                    if (confirmSleep) T(t("PC-en sover – Olympus er borte til den våkner."), color = Z.bad, size = 11)
                }
            }
        }
        item {
            Group(t("▣ STATUS")) {
                val i = info
                if (i == null) T(t("henter …"), Modifier.padding(horizontal = 16.dp), color = Z.muted)
                else {
                    val gpu = i.optJSONObject("gpu")
                    fun deg(v: Double?) = v?.takeIf { !it.isNaN() }?.let { "%.0f°".format(it) } ?: "?"
                    ListRow(t("GPU %s · CPU %s · disk %s %", deg(gpu?.optDouble("temp")), deg(i.optJSONObject("cpu")?.optDouble("temp")), i.optInt("disk_pct")),
                        icon = "●", iconColor = Z.ok, subtitle = listOfNotNull(i.optString("host"), gpu?.optString("name")?.takeIf { it.isNotEmpty() },
                            gpu?.takeIf { it.has("vram_total_gb") }?.let { "VRAM %.1f/%.0f GB".format(it.optDouble("vram_used_gb"), it.optDouble("vram_total_gb")) }).joinToString(" · "))
                    i.optJSONObject("services")?.let { sv ->
                        val names = mapOf("olympus-web" to "server", "transmission" to "nedlasting", "tailscaled" to "Tailscale")
                        Row(Modifier.padding(horizontal = 16.dp, vertical = 8.dp), horizontalArrangement = Arrangement.spacedBy(14.dp)) {
                            T("▣", Modifier.width(22.dp), color = Z.gold)
                            sv.keys().forEach { k ->
                                Row { T("● ", color = if (sv.optBoolean(k)) Z.ok else Z.bad, size = 12); T(names[k] ?: k, size = 12) }
                            }
                        }
                    }
                }
            }
        }
        item {
            Group(t("☐ TELEFONER MED TILGANG")) {
                devices.forEach { dv ->
                    val name = dv.optString("name")
                    ListRow(if (name.startsWith("sdk_")) "emulator" else name, icon = "◦",
                        subtitle = t("sist sett %s", java.text.SimpleDateFormat("dd.MM HH:mm", NB).format(java.util.Date((dv.optDouble("last_seen", dv.optDouble("created")) * 1000).toLong()))))
                }
            }
        }
        item { Spacer(Modifier.height(12.dp)) }
    }
}

fun shortSink(label: String): String = label.replace(Regex("\\s*\\[[^\\]]*\\]"), "")
    .replace(" Digital Stereo", "").replace(" Analog Stereo", "").replace(" Audio Controller", "").replace(" (IEC958)", "").trim()
