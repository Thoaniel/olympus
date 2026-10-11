package no.olympus.zeus

import android.content.Context
import android.net.Uri
import android.provider.OpenableColumns
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
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
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MultipartBody
import okhttp3.Request
import okhttp3.RequestBody
import okhttp3.RequestBody.Companion.toRequestBody
import okio.BufferedSink
import okio.source
import org.json.JSONObject

/* Alt du kan be Zeus om i biblioteket, kan du også gjøre selv her: legge til fra lenke eller fil, se nedlastinger,
 * redigere, bytte cover og slette. Samme handlinger på serveren som Zeus bruker (én motor, to måter å styre den). */

val KINDS = listOf("music" to "musikk", "film" to "film", "show" to "serie", "audiobook" to "lydbok", "ebook" to "e-bok")
fun kindLabel(k: String) = KINDS.firstOrNull { it.first == k }?.second ?: k

/** Lim inn en lenke (eller del den fra en annen app) → serveren sjekker hva det er → du velger og starter. */
@Composable
fun AddMediaDialog(hub: Hub, initialUrl: String, defaultKind: String, onClose: () -> Unit, onStarted: () -> Unit) {
    val scope = rememberCoroutineScope()
    val ctx = LocalContext.current
    var url by remember { mutableStateOf(initialUrl) }
    var info by remember { mutableStateOf<JSONObject?>(null) }
    var kind by remember { mutableStateOf(defaultKind) }
    var title by remember { mutableStateOf("") }
    var busy by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf<String?>(null) }
    var uploading by remember { mutableStateOf<String?>(null) }
    var mode by remember { mutableStateOf(if (initialUrl.isBlank()) t("søk") else "lenke") }
    var query by remember { mutableStateOf("") }
    var hits by remember { mutableStateOf(emptyList<JSONObject>()) }
    fun search() {
        if (query.isBlank()) return
        busy = true; error = null
        scope.launch {
            runCatching { hub.api.get("/find?q=" + java.net.URLEncoder.encode(query.trim(), "UTF-8") + "&kind=" + kind) }
                .onSuccess { r -> hits = r.optJSONArray("results").list { it }; if (hits.isEmpty()) error = t("Ingen treff") }
                .onFailure { error = it.message }
            busy = false
        }
    }
    fun inspect() {
        if (url.isBlank()) return
        busy = true; error = null; info = null
        scope.launch {
            runCatching { hub.api.post("/media/inspect", JSONObject().put("url", url.trim())) }
                .onSuccess { i -> info = i; title = i.optString("title"); i.optString("kind").takeIf { k -> KINDS.any { it.first == k } }?.let { kind = it } }
                .onFailure { error = it.message ?: t("Fikk ikke sjekket lenken") }
            busy = false
        }
    }
    LaunchedEffect(initialUrl) { if (initialUrl.isNotBlank()) inspect() }
    val pick = rememberLauncherForActivityResult(ActivityResultContracts.GetContent()) { uri ->
        if (uri != null) {
            uploading = displayName(ctx, uri)
            scope.launch {
                val err = upload(ctx, hub, uri, kind, title)
                uploading = null
                if (err == null) { hub.toast = t("Lastet opp – dukker opp i biblioteket straks"); hub.mediaVersion++; onStarted() } else error = err
            }
        }
    }
    androidx.compose.ui.window.Dialog(onDismissRequest = onClose) {
        Column(Modifier.fillMaxWidth().background(Z.bg).border(1.dp, Z.gold).padding(16.dp).verticalScroll(rememberScrollState()),
            verticalArrangement = Arrangement.spacedBy(10.dp)) {
            T(t("+ LEGG TIL"), color = Z.gold, bold = true, glow = true)
            Seg(listOf(t("søk"), "lenke", "plate"), mode, Modifier.padding(horizontal = 0.dp)) { mode = it; error = null }
            if (mode == "plate") RipPanel(hub) else if (mode == "søk") {
                T(t("Søker i Internet Archive, LibriVox, Gutenberg, Wikimedia Commons og Jamendo."), color = Z.muted, size = 11)
                Seg(KINDS.map { it.second }, kindLabel(kind), Modifier.padding(horizontal = 0.dp)) { l -> kind = KINDS.first { it.second == l }.first }
                Field(query, { query = it }, t("hva leter du etter?"), Modifier.fillMaxWidth(), onDone = ::search)
                Btn(if (busy) t("søker …") else t("[ søk ]"), Modifier.fillMaxWidth(), gold = true) { if (!busy) search() }
                error?.let { T(t("✗ %s", it), color = Z.bad, size = 12) }
                hits.forEach { h ->
                    Column(Modifier.fillMaxWidth().border(1.dp, Z.line).clickable {
                        scope.launch {
                            runCatching { hub.api.post("/media/add", JSONObject().put("id", h.optString("id")).put("kind", h.optString("kind").ifEmpty { kind })) }
                                .onSuccess { hub.toast = t("Laster ned «%s»", h.optString("title")); hub.mediaVersion++; onStarted() }
                                .onFailure { error = it.message }
                        }
                    }.padding(10.dp)) {
                        T(h.optString("title"), color = Z.text, maxLines = 2)
                        T(listOfNotNull(h.optString("creator").takeIf { it.isNotEmpty() }, h.optInt("year").takeIf { it > 0 }?.toString(),
                            h.optString("source"), h.optLong("size").takeIf { it > 0 }?.let { size(it) }, h.optString("license").takeIf { it.isNotEmpty() })
                            .joinToString(" · "), color = Z.muted, size = 11, maxLines = 2)
                        T(t("trykk for å laste ned"), color = Z.gold, size = 11)
                    }
                }
                Btn(t("[ lukk ]"), Modifier.fillMaxWidth()) { onClose() }
                return@Column
            }
            T(t("Lim inn en lenke (YouTube, SoundCloud, archive.org, magnet, .torrent eller en fil på nett) – eller last opp fra telefonen."),
                color = Z.muted, size = 11)
            Field(url, { url = it; info = null }, "https://… eller magnet:?…", Modifier.fillMaxWidth(), onDone = ::inspect)
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Btn(if (busy) t("sjekker …") else t("[ sjekk lenken ]"), Modifier.weight(1f), gold = true) { if (!busy) inspect() }
                Btn(if (uploading != null) t("laster opp …") else t("[ fra telefonen ]"), Modifier.weight(1f)) { if (uploading == null) pick.launch("*/*") }
            }
            uploading?.let { T(t("↑ %s", it), color = Z.gold, size = 12) }
            error?.let { T(t("✗ %s", it), color = Z.bad, size = 12) }
            info?.let { i ->
                Column(Modifier.fillMaxWidth().border(1.dp, Z.line).padding(12.dp),
                    verticalArrangement = Arrangement.spacedBy(6.dp)) {
                    T(i.optString("title").ifEmpty { t("(uten tittel)") }, color = Z.gold, bold = true, glow = true, maxLines = 2)
                    T(listOfNotNull(i.optString("artist").takeIf { it.isNotEmpty() && it != "null" },
                        i.optInt("year").takeIf { it > 0 }?.toString(),
                        i.optLong("size").takeIf { it > 0 }?.let { size(it) },
                        i.optDouble("duration").takeIf { !it.isNaN() && it > 0 }?.let { clock(it) },
                        i.optString("source"), when (i.optString("method")) { "torrent" -> "torrent"; "ytdlp" -> t("strømmetjeneste"); else -> t("direkte fil") })
                        .joinToString(" · "), color = Z.muted, size = 12)
                    (0 until (i.optJSONArray("warnings")?.length() ?: 0)).forEach { T("⚠ " + i.getJSONArray("warnings").getString(it), color = Z.bad, size = 12) }
                }
                T(t("Hva er det?"), color = Z.muted, size = 11)
                Seg(KINDS.map { it.second }, kindLabel(kind), Modifier.padding(horizontal = 0.dp)) { l -> kind = KINDS.first { it.second == l }.first }
                Field(title, { title = it }, "tittel", Modifier.fillMaxWidth())
                Btn(t("[ last ned ]"), Modifier.fillMaxWidth(), solid = true) {
                    busy = true
                    scope.launch {
                        runCatching { hub.api.post("/media/add", JSONObject().put("url", url.trim()).put("kind", kind).put("title", title)) }
                            .onSuccess { hub.toast = t("Laster ned «%s» – se ↓ nedlastinger", title.ifEmpty { "lenken" }); hub.mediaVersion++; onStarted() }
                            .onFailure { error = it.message }
                        busy = false
                    }
                }
            }
            Btn(t("[ avbryt ]"), Modifier.fillMaxWidth()) { onClose() }
        }
    }
}

private fun size(b: Long): String = when {
    b >= 1_000_000_000 -> "%.1f GB".format(b / 1e9)
    b >= 1_000_000 -> "%.0f MB".format(b / 1e6)
    else -> "%.0f kB".format(b / 1e3)
}

private fun displayName(ctx: Context, uri: Uri): String =
    ctx.contentResolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME), null, null, null)?.use { c ->
        if (c.moveToFirst()) c.getString(0) else null
    } ?: uri.lastPathSegment.orEmpty()

/** Last opp en fil fra telefonen rett inn i biblioteket – strømmet, så store filmer ikke fyller minnet. */
private suspend fun upload(ctx: Context, hub: Hub, uri: Uri, kind: String, title: String): String? = withContext(Dispatchers.IO) {
    val name = displayName(ctx, uri)
    val len = ctx.contentResolver.query(uri, arrayOf(OpenableColumns.SIZE), null, null, null)?.use { c -> if (c.moveToFirst()) c.getLong(0) else -1L } ?: -1L
    val type = (ctx.contentResolver.getType(uri) ?: "application/octet-stream").toMediaType()
    val body = object : RequestBody() {
        override fun contentType() = type
        override fun contentLength() = len
        override fun writeTo(sink: BufferedSink) {
            ctx.contentResolver.openInputStream(uri)!!.source().use { sink.writeAll(it) }
        }
    }
    val form = MultipartBody.Builder().setType(MultipartBody.FORM)
        .addFormDataPart("kind", kind).apply { if (title.isNotBlank()) addFormDataPart("title", title) }
        .addFormDataPart("file", name, body).build()
    val client = authedClient(hub.store).newBuilder().writeTimeout(30, java.util.concurrent.TimeUnit.MINUTES).build()
    runCatching {
        client.newCall(Request.Builder().url("http://${hub.store.host}/api/v1/media/upload").post(form).build()).execute().use { r ->
            if (!r.isSuccessful) error(runCatching { JSONObject(r.body!!.string()).optString("error") }.getOrNull()?.ifEmpty { null } ?: "HTTP ${r.code}")
        }
    }.exceptionOrNull()?.message
}

/** ↓ nedlastinger: framdrift live fra PC-en, avbryt, prøv igjen og fjern. */
@Composable
fun DownloadsView(hub: Hub, onBack: () -> Unit) {
    val scope = rememberCoroutineScope()
    LaunchedEffect(Unit) { hub.refreshDownloads() }
    fun act(d: JSONObject, how: String) = scope.launch {
        runCatching {
            when (how) {
                "cancel" -> hub.api.delete("/downloads/${d.optString("id")}")
                "retry" -> hub.api.post("/downloads/${d.optString("id")}/retry")
                else -> hub.api.delete("/downloads/${d.optString("id")}?forget=1")
            }
        }.onFailure { hub.toast = t("Fikk ikke til: %s", it.message) }
        hub.refreshDownloads()
    }
    Column(Modifier.fillMaxSize()) {
        SubHeader(t("↓ nedlastinger"), onBack) {
            Box(Modifier.clickable {
                scope.launch { runCatching { hub.api.post("/media/scan") }; hub.mediaVersion++; hub.toast = t("Biblioteket er skannet") }
            }.padding(10.dp)) { T(t("⟳ skann"), color = Z.muted, size = 12) }
        }
        if (hub.downloads.isEmpty()) { EmptyArt(Ascii.VINYL, t("Ingen nedlastinger. Legg til med «+ legg til» – eller be Zeus finne noe.")); return }
        LazyColumn {
            items(hub.downloads, key = { it.optString("id") }) { d ->
                val st = d.optString("status")
                // Statusene fra Olympus: laster ned · lastet ned (sorteres inn) · deler · ferdig · feilet · avbrutt
                val active = st in listOf(t("laster ned"), t("lastet ned"))
                val failed = st in listOf("feilet", "avbrutt")
                Column(Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 8.dp)) {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        T(if (failed) "✗" else if (active) "↓" else if (st == "deler") "⇅" else "✓", color = if (failed) Z.bad else if (active) Z.gold else Z.ok)
                        Spacer(Modifier.padding(4.dp))
                        T(d.optString("title"), Modifier.weight(1f), maxLines = 1)
                        T("${kindLabel(d.optString("kind"))} · $st", color = Z.muted, size = 11)
                    }
                    if (active) T("${blocks(d.optDouble("progress", 0.0).coerceIn(0.0, 1.0), 22)} ${(100 * d.optDouble("progress", 0.0)).toInt()} %",
                        color = Z.gold, size = 11)
                    if (failed && d.optString("error").isNotEmpty()) T(d.optString("error"), color = Z.bad, size = 11, maxLines = 2)
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp), modifier = Modifier.padding(top = 4.dp)) {
                        if (active) Btn(t("avbryt"), danger = true) { act(d, "cancel") }
                        if (failed) Btn(t("prøv igjen"), gold = true) { act(d, "retry") }
                        if (!active) Btn(t("fjern fra lista")) { act(d, "forget") }
                    }
                }
            }
        }
    }
}

/** Hold inne på et album, en film eller en bok: rediger, bytt cover, hent cover på nytt, slett. */
@Composable
fun ItemSheet(hub: Hub, id: String, onClose: () -> Unit) {
    val scope = rememberCoroutineScope()
    var item by remember { mutableStateOf<JSONObject?>(null) }
    var mode by remember { mutableStateOf("menu") }
    var confirmDelete by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf<String?>(null) }
    val f = remember { mutableStateOf(mapOf<String, String>()) }
    val orig = remember { mutableStateOf(mapOf<String, String>()) }   // det som sto der – bare endrede felt sendes
    val ctx = LocalContext.current
    LaunchedEffect(id) {
        runCatching { hub.api.get("/media/item/$id") }.onSuccess { i ->
            item = i
            f.value = listOf("title", "artist", "album", "year", "genre").associateWith { k -> i.optString(k).takeIf { it != "null" && it != "0" }.orEmpty() }
            orig.value = f.value
        }.onFailure { error = it.message }
    }
    val pickImage = rememberLauncherForActivityResult(ActivityResultContracts.GetContent()) { uri ->
        if (uri != null) scope.launch {
            val err = withContext(Dispatchers.IO) {
                runCatching {
                    val bytes = ctx.contentResolver.openInputStream(uri)!!.use { it.readBytes() }
                    val type = ctx.contentResolver.getType(uri) ?: "image/jpeg"
                    authedClient(hub.store).newCall(Request.Builder().url("http://${hub.store.host}/api/v1/media/item/$id/cover")
                        .put(bytes.toRequestBody(type.toMediaType())).build()).execute().use { r -> if (!r.isSuccessful) error("HTTP ${r.code}") }
                }.exceptionOrNull()?.message
            }
            if (err == null) { hub.toast = t("Nytt cover"); hub.mediaVersion++; onClose() } else error = err
        }
    }
    androidx.compose.ui.window.Dialog(onDismissRequest = onClose) {
        Column(Modifier.fillMaxWidth().background(Z.bg).border(1.dp, Z.gold).padding(vertical = 12.dp).verticalScroll(rememberScrollState())) {
            val i = item
            Row(Modifier.padding(horizontal = 16.dp), verticalAlignment = Alignment.CenterVertically) {
                T(i?.optString("title") ?: t("henter …"), Modifier.weight(1f), color = Z.gold, bold = true, glow = true, maxLines = 1)
                Box(Modifier.clickable(onClick = onClose).padding(8.dp)) { T("✕", color = Z.muted) }
            }
            if (i != null) T(listOfNotNull(i.optString("type"), i.optString("artist").takeIf { it.isNotEmpty() && it != "null" },
                "${i.optInt("files")} filer", i.optLong("size").takeIf { it > 0 }?.let { size(it) }, i.optString("folder")).joinToString(" · "),
                Modifier.padding(horizontal = 16.dp), color = Z.muted, size = 11)
            error?.let { T(t("✗ %s", it), Modifier.padding(horizontal = 16.dp), color = Z.bad, size = 12) }
            when (mode) {
                "edit" -> Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    listOf("title" to "tittel", "artist" to "artist", "album" to "album", "year" to t("år"), "genre" to "sjanger").forEach { (k, hint) ->
                        Field(f.value[k].orEmpty(), { v -> f.value = f.value + (k to v) }, hint, Modifier.fillMaxWidth())
                    }
                    Btn(t("[ lagre ]"), Modifier.fillMaxWidth(), solid = true) {
                        val body = JSONObject()
                        // Bare feltene du faktisk endret: ellers ble f.eks. albumets sjanger og år skrevet over alle sporene
                        f.value.forEach { (k, v) ->
                            if (v.trim() != orig.value[k].orEmpty().trim() && v.isNotBlank()) {
                                if (k == "year") v.toIntOrNull()?.let { body.put(k, it) } else body.put(k, v.trim())
                            }
                        }
                        if (body.length() == 0) { onClose(); return@Btn }
                        scope.launch {
                            runCatching { hub.api.patch("/media/item/$id", body) }
                                .onSuccess { hub.toast = "Lagret"; hub.mediaVersion++; onClose() }.onFailure { error = it.message }
                        }
                    }
                }
                "cover" -> {
                    var link by remember { mutableStateOf("") }
                    Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                        Btn(t("[ velg bilde fra telefonen ]"), Modifier.fillMaxWidth(), gold = true) { pickImage.launch("image/*") }
                        Field(link, { link = it }, t("eller lim inn en bildelenke"), Modifier.fillMaxWidth())
                        Btn(t("[ bruk lenken ]"), Modifier.fillMaxWidth()) {
                            scope.launch {
                                runCatching { hub.api.put("/media/item/$id/cover", JSONObject().put("url", link.trim())) }
                                    .onSuccess { hub.toast = t("Nytt cover"); hub.mediaVersion++; onClose() }.onFailure { error = it.message }
                            }
                        }
                    }
                }
                else -> {
                    ListRow(t("Rediger"), icon = "✎", subtitle = t("tittel, artist, album, år, sjanger")) { mode = "edit" }
                    ListRow(t("Bytt cover"), icon = "▣", subtitle = t("bilde fra telefonen eller en lenke")) { mode = "cover" }
                    ListRow(t("Hent cover på nytt"), icon = "⟳", subtitle = t("MusicBrainz / Wikipedia")) {
                        scope.launch {
                            runCatching { hub.api.post("/media/item/$id/cover/fetch") }
                                .onSuccess { hub.toast = if (it.optBoolean("found")) t("Fant et cover") else t("Fant ikke noe cover"); hub.mediaVersion++; onClose() }
                                .onFailure { error = it.message }
                        }
                    }
                    ListRow(if (confirmDelete) t("Trykk igjen: slett (legges i papirkurven)") else "Slett", icon = "✕", iconColor = Z.bad, on = confirmDelete) {
                        if (!confirmDelete) confirmDelete = true
                        else scope.launch {
                            runCatching { hub.api.delete("/media/item/$id") }
                                .onSuccess { hub.toast = t("Slettet %s – ligger i papirkurven på PC-en", it.optString("what")); hub.mediaVersion++; onClose() }
                                .onFailure { error = it.message }
                        }
                    }
                }
            }
            Spacer(Modifier.height(4.dp))
        }
    }
}


// --- Zeus: samtaler og minne – det Zeus vet og har snakket om, synlig og redigerbart for deg ----------
@Composable
fun SessionsView(hub: Hub, onBack: () -> Unit) {
    val scope = rememberCoroutineScope()
    var list by remember { mutableStateOf(emptyList<JSONObject>()) }
    suspend fun load() { list = runCatching { hub.api.get("/sessions?limit=40") }.getOrNull()?.optJSONArray("items").list { it } }
    LaunchedEffect(hub.metaVersion) { load() }
    var confirm by remember { mutableStateOf<String?>(null) }
    Column(Modifier.fillMaxSize()) {
        SubHeader(t("≡ samtaler"), onBack) {
            Box(Modifier.clickable {
                scope.launch { runCatching { hub.api.post("/sessions/new") }.onSuccess { hub.chat.clear(); onBack() }.onFailure { hub.toast = it.message } }
            }.padding(10.dp)) { T(t("+ ny"), color = Z.gold, size = 12) }
        }
        if (list.isEmpty()) { EmptyArt(Ascii.ZEUS_OFF.lines().take(1).joinToString(), t("Ingen lagrede samtaler ennå.")); return }
        LazyColumn {
            items(list, key = { it.optString("id") }) { x ->
                val id = x.optString("id")
                ListRow(x.optString("title").ifEmpty { t("(uten tittel)") }, icon = "≡",
                    subtitle = java.text.SimpleDateFormat("dd.MM HH:mm", java.util.Locale.ROOT).format(java.util.Date((x.optDouble("updated") * 1000).toLong())) +
                        t(" · %s meldinger", x.optInt("messages")) + if (confirm == id) t(" · hold inne igjen for å slette") else "",
                    onLongClick = {
                        if (confirm == id) scope.launch { runCatching { hub.api.delete("/sessions/$id") }; confirm = null; load() } else confirm = id
                    }) {
                    scope.launch {
                        runCatching { hub.api.post("/sessions/$id/open") }.onSuccess { onBack() }
                            .onFailure { hub.toast = t("Kan ikke bytte mens Zeus svarer") }
                    }
                }
            }
        }
    }
}

@Composable
fun MemoryView(hub: Hub, onBack: () -> Unit) {
    val scope = rememberCoroutineScope()
    var data by remember { mutableStateOf<JSONObject?>(null) }
    var editing by remember { mutableStateOf<Pair<String, String>?>(null) }   // (id eller "new:<target>", tekst)
    suspend fun load() { data = runCatching { hub.api.get("/memory") }.getOrNull() }
    LaunchedEffect(hub.metaVersion) { load() }
    Column(Modifier.fillMaxSize()) {
        SubHeader(t("◈ det Zeus husker"), onBack)
        LazyColumn {
            listOf("user" to t("OM DEG"), "memory" to t("ZEUS' NOTATER")).forEach { (target, label) ->
                item {
                    Group(label, action = t("+ legg til"), onAction = { editing = "new:$target" to "" }) {
                        val items = data?.optJSONArray(target).list { it }
                        if (items.isEmpty()) T(t("tomt"), Modifier.padding(horizontal = 16.dp), color = Z.muted, size = 12)
                        items.forEach { m -> ListRow(m.optString("text"), icon = "·") { editing = m.optString("id") to m.optString("text") } }
                    }
                }
            }
            item { T(t("Trykk på noe for å endre eller slette det. Zeus bruker dette i alle samtaler."), Modifier.padding(16.dp), color = Z.dim, size = 11) }
        }
    }
    editing?.let { (id, text) ->
        var t by remember(id) { mutableStateOf(text) }
        androidx.compose.ui.window.Dialog(onDismissRequest = { editing = null }) {
            Column(Modifier.fillMaxWidth().background(Z.bg).border(1.dp, Z.gold).padding(16.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
                T(if (id.startsWith("new:")) t("◈ NYTT MINNE") else t("◈ ENDRE MINNE"), color = Z.gold, bold = true, glow = true)
                Field(t, { t = it }, t("f.eks. «jeg liker jazz om kvelden»"), Modifier.fillMaxWidth())
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    Btn(t("[ lagre ]"), Modifier.weight(1f), solid = true) {
                        scope.launch {
                            runCatching {
                                if (id.startsWith("new:")) hub.api.post("/memory", JSONObject().put("target", id.removePrefix("new:")).put("text", t.trim()))
                                else hub.api.patch("/memory/$id", JSONObject().put("text", t.trim()))
                            }.onFailure { hub.toast = it.message }
                            editing = null; load()
                        }
                    }
                    if (!id.startsWith("new:")) Btn(t("slett"), danger = true) {
                        scope.launch { runCatching { hub.api.delete("/memory/$id") }; editing = null; load() }
                    }
                }
            }
        }
    }
}


/** Platen i PC-ens plateleser: lyd-CD → FLAC (navn fra MusicBrainz), DVD → hovedfilmen som MKV. Fremdriften
 *  oppdateres hvert 3. sekund mens den ripper. */
@Composable
fun RipPanel(hub: Hub) {
    val scope = rememberCoroutineScope()
    var st by remember { mutableStateOf<JSONObject?>(null) }
    var title by remember { mutableStateOf("") }
    var tick by remember { mutableStateOf(0) }
    LaunchedEffect(tick) { st = runCatching { hub.api.get("/media/rip") }.getOrNull() }
    val job = st?.optJSONObject("job")
    LaunchedEffect(job?.optString("status")) {
        while (job?.optString("status") in listOf("starter", "ripper")) { kotlinx.coroutines.delay(3000); tick++ }
    }
    val drives = st?.optJSONArray("drives").list { it }
    when {
        st == null -> T(t("ser i plateleseren …"), color = Z.muted, size = 12)
        drives.isEmpty() -> T(t("PC-en har ingen plateleser tilkoblet (en USB-plateleser virker også)."), color = Z.muted, size = 12)
        else -> drives.forEach { d ->
            val kind = d.optString("kind")
            T(t("◉ %s: ", d.optString("dev")) + mapOf("audio" to "lyd-CD", "dvd" to "DVD", "data" to "dataplate", "empty" to "tom")
                .getOrDefault(kind, kind) + d.optString("label").takeIf { it.isNotBlank() }?.let { t(" «%s»", it) }.orEmpty())
            if (kind == "dvd") {
                Field(title, { title = it }, t("navn på filmen (valgfritt)"), Modifier.fillMaxWidth())
                if (st?.optBoolean("dvdcss") == false)
                    T(t("Kjøpte DVD-er er ofte kopibeskyttet – da trengs libdvdcss på PC-en."), color = Z.dim, size = 11)
            }
            if (kind == "audio" || kind == "dvd") Btn(t("[ ◉ ripp inn i biblioteket ]"), Modifier.fillMaxWidth(), gold = true) {
                scope.launch {
                    runCatching { hub.api.post("/media/rip", JSONObject().put("dev", d.optString("dev")).put("title", title)) }
                        .onSuccess { tick++ }.onFailure { hub.toast = it.message ?: t("Fikk ikke startet") }
                }
            }
        }
    }
    job?.let { j ->
        val pct = (j.optDouble("progress") * 100).toInt()
        T("${j.optString("title")}: ${j.optString("status")} $pct %" + j.optString("error").takeIf { it.isNotBlank() }?.let { t(" – %s", it) }.orEmpty(),
            color = if (j.optString("status") == "feilet") Z.bad else Z.gold, size = 12)
    }
}
