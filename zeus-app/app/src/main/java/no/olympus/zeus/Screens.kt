package no.olympus.zeus

import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.launch
import org.json.JSONObject
import java.time.LocalDate
import java.time.LocalTime
import java.time.format.TextStyle as JTextStyle
import java.util.Locale

private val NB = Locale.forLanguageTag("nb-NO")

// --- små byggeklosser ------------------------------------------------------------------------------
@Composable
fun T(text: String, modifier: Modifier = Modifier, color: androidx.compose.ui.graphics.Color = Z.text, size: Int = 13,
      bold: Boolean = false, glow: Boolean = false, align: TextAlign? = null, maxLines: Int = Int.MAX_VALUE) {
    androidx.compose.foundation.text.BasicText(
        text, modifier,
        style = Z.body.copy(color = color, fontSize = size.sp, lineHeight = (size + 6).sp,
            fontWeight = if (bold) FontWeight.Bold else FontWeight.Normal, shadow = Z.glow(glow),
            textAlign = align ?: TextAlign.Unspecified),
        maxLines = maxLines, overflow = TextOverflow.Ellipsis,  // «…» i stedet for et halvt ord
    )
}

@Composable
fun Rule(modifier: Modifier = Modifier, fill: String = "─", color: androidx.compose.ui.graphics.Color = Z.line) =
    T(fill.repeat(200), modifier, color = color, maxLines = 1)

@Composable
fun Btn(label: String, modifier: Modifier = Modifier, gold: Boolean = false, solid: Boolean = false, danger: Boolean = false,
        onClick: () -> Unit) {
    val border = when { danger -> Z.bad; gold || solid -> Z.gold; else -> Z.line }
    Box(
        modifier.heightIn(min = 44.dp).background(if (solid) Z.gold else Z.bg).border(1.dp, border)
            .clickable(onClick = onClick).padding(horizontal = 12.dp),
        contentAlignment = Alignment.Center,
    ) { T(label, color = if (solid) Z.bg else if (danger) Z.bad else if (gold) Z.gold else Z.text, bold = solid) }
}

@Composable
fun Section(title: String, modifier: Modifier = Modifier) {
    Row(modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
        T(title, color = Z.gold, size = 12)
        Spacer(Modifier.width(8.dp))
        Rule(Modifier.weight(1f))
    }
}

@Composable
fun Field(value: String, onChange: (String) -> Unit, hint: String, modifier: Modifier = Modifier, onDone: (() -> Unit)? = null) {
    Box(modifier.border(1.dp, Z.line).padding(10.dp)) {
        if (value.isEmpty()) T(hint, color = Z.muted)
        BasicTextField(
            value, onChange, Modifier.fillMaxWidth(), textStyle = Z.body, cursorBrush = SolidColor(Z.gold),
            singleLine = onDone != null,  // Enter skal sende/søke, ikke lage linjeskift
            keyboardOptions = KeyboardOptions(imeAction = if (onDone != null) ImeAction.Send else ImeAction.Next),
            keyboardActions = KeyboardActions(onSend = { onDone?.invoke() }),
        )
    }
}

// --- paring --------------------------------------------------------------------------------------
@Composable
fun PairScreen(hub: Hub, initial: String?, onCancel: (() -> Unit)? = null) {
    val scope = rememberCoroutineScope()
    var link by remember { mutableStateOf(initial.orEmpty()) }
    var error by remember { mutableStateOf<String?>(null) }
    var working by remember { mutableStateOf(false) }
    fun go() {
        working = true
        scope.launch { error = hub.pair(link); working = false }
    }
    // En lenke fra QR/nettleser fylles bare inn – du ser adressen og trykker selv (ingen automatisk tilkobling).
    LaunchedEffect(initial) { if (!initial.isNullOrEmpty()) link = initial }
    Column(Modifier.fillMaxSize().statusBarsPadding().imePadding().padding(24.dp), verticalArrangement = Arrangement.spacedBy(18.dp),
        horizontalAlignment = Alignment.CenterHorizontally) {
        Spacer(Modifier.height(40.dp))
        Art(Ascii.LOGO, Modifier.glitch(), size = 10f)
        T(t("O L Y M P U S   L A B ' S"), color = Z.muted, size = 11)
        Spacer(Modifier.height(20.dp))
        T(t("Koble til Olympus på PC-en:\n\n1. kjør `olympus pair` på PC-en\n2. skann QR-koden med kameraet\n   (eller lim inn lenken under)"), color = Z.text)
        Field(link, { link = it }, "zeus://pair?…", Modifier.fillMaxWidth(), onDone = ::go)
        val host = Hub.hostOf(link)
        if (host.isNotEmpty()) {
            val ok = Hub.isTailscale(host)
            T(if (ok) t("Kobler til Olympus på %s", host) else t("⚠ %s er ikke en Tailscale-adresse – avvises", host),
                color = if (ok) Z.gold else Z.bad)
            if (hub.store.paired) T(t("Dette erstatter tilkoblingen til %s.", hub.store.host), color = Z.bad, size = 12)
        }
        Btn(if (working) t("kobler til …") else t("[ koble til ]"), Modifier.fillMaxWidth(), solid = true) { if (!working) go() }
        onCancel?.let { Btn(t("[ avbryt ]"), Modifier.fillMaxWidth()) { it() } }
        error?.let { T(t("✗ %s", it), color = Z.bad) }
        Spacer(Modifier.weight(1f))
        T(t("Tailscale må være på, på både PC og telefon."), color = Z.muted, size = 11)
    }
}

// --- lås -----------------------------------------------------------------------------------------
@Composable
fun LockScreen(message: String?, server: String, onUnlock: () -> Unit) {
    // Telefonens egen fingeravtrykksdialog kommer av seg selv (den viser sensoren der den faktisk er). Bak den
    // står logoen og et ASCII-fingeravtrykk som puster – trykk på det hvis dialogen ble lukket.
    Box(Modifier.fillMaxSize()) {
        Column(Modifier.fillMaxSize().padding(24.dp), horizontalAlignment = Alignment.CenterHorizontally) {
            Spacer(Modifier.weight(0.8f))
            Art(Ascii.LOGO, Modifier.glitch(), size = 10f)
            Spacer(Modifier.height(14.dp))
            T(t("O L Y M P U S   L A B ' S"), color = Z.muted, size = 11)
            Spacer(Modifier.weight(1f))
            Row(verticalAlignment = Alignment.CenterVertically) { T(t("trykk på fingeravtrykket"), color = Z.text); Cursor() }
            T("↓", Modifier.padding(top = 6.dp).breathe(), color = Z.gold, size = 16, glow = true)
            Spacer(Modifier.height(18.dp))
            Box(Modifier.waves().clickable(onClick = onUnlock).padding(44.dp), contentAlignment = Alignment.Center) {
                Art(Ascii.FINGERPRINT, Modifier.breathe(), size = 12f, bold = true)
            }
            if (message != null) T(message, Modifier.padding(top = 18.dp), color = Z.bad, align = TextAlign.Center)
            Spacer(Modifier.weight(0.6f))
        }
        Row(Modifier.align(Alignment.BottomCenter).navigationBarsPadding().padding(bottom = 22.dp)) {
            T(server.ifEmpty { "olympus" }, color = Z.muted, size = 11); T(" ● ", color = Z.ok, size = 11); T(t("via Tailscale"), color = Z.muted, size = 11)
        }
    }
}

// --- morgenbrief ---------------------------------------------------------------------------------
@Composable
fun BriefScreen(hub: Hub, onDone: (String) -> Unit) {
    var data by remember { mutableStateOf<JSONObject?>(null) }
    var loaded by remember { mutableStateOf(false) }
    LaunchedEffect(Unit) { data = hub.brief(); loaded = true }
    val today = LocalDate.now()
    Column(Modifier.fillMaxSize().statusBarsPadding().navigationBarsPadding().padding(22.dp), verticalArrangement = Arrangement.spacedBy(14.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            T(t("ϟ ZEUS"), color = Z.gold, bold = true, glow = true)
            Spacer(Modifier.width(8.dp)); Rule(Modifier.weight(1f)); Spacer(Modifier.width(8.dp))
            T("${today.dayOfWeek.getDisplayName(JTextStyle.SHORT, NB)} ${today.dayOfMonth}. ${today.month.getDisplayName(JTextStyle.SHORT, NB)}", color = Z.muted, size = 12)
        }
        val hour = LocalTime.now().hour
        Typed(if (hour < 5) t("GOD NATT.") else if (hour < 10) t("GOD MORGEN.") else if (hour < 18) t("GOD DAG.") else t("GOD KVELD."))
        Column(Modifier.rise(600).fillMaxWidth().border(1.dp, Z.line).padding(12.dp), verticalArrangement = Arrangement.spacedBy(2.dp)) {
            T(t("I DAG"), color = Z.gold)
            val evs = hub.events.filter { it.date == today.toString() }
            if (evs.isEmpty()) T(t("ingenting i planen"), color = Z.muted)
            evs.forEach { T("${it.time.padEnd(10)} ${it.title}", maxLines = 1) }
            // Tom dag: vis det som kommer de neste sju dagene, så briefen sier noe nyttig.
            val soon = hub.events.filter { it.date > today.toString() && it.date <= today.plusDays(7).toString() }.take(3)
            if (evs.isEmpty() && soon.isNotEmpty()) {
                T(t("SNART"), Modifier.padding(top = 8.dp), color = Z.gold, size = 12)
                soon.forEach { e ->
                    val d = LocalDate.parse(e.date)
                    val day = if (d == today.plusDays(1)) t("i morgen") else d.dayOfWeek.getDisplayName(JTextStyle.SHORT, NB)
                    T("${day.padEnd(9)} ${if (e.allDay) "" else e.time + " "}${e.title}", color = Z.muted, maxLines = 1)
                }
            }
        }
        val w = data?.optJSONObject("weather")
        val scope = rememberCoroutineScope()
        var editPlace by remember { mutableStateOf(false) }
        var place by remember { mutableStateOf("") }
        var placeMsg by remember { mutableStateOf("") }
        Column(Modifier.rise(900).fillMaxWidth().border(1.dp, Z.line).clickable(enabled = loaded && data != null) { editPlace = !editPlace }
            .padding(12.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(14.dp)) {
                Art(Ascii.weather(w?.optString("symbol").orEmpty()), color = if (w != null) Z.gold else Z.muted, size = 9f, glow = w != null)
                Column(Modifier.weight(1f)) {
                    T(t("VÆR") + (w?.optString("place")?.takeIf { it.isNotEmpty() }?.let { t(" · %s", it) } ?: ""), color = Z.gold, maxLines = 1)
                    if (w != null) T("${w.optDouble("temp").let { if (it.isNaN()) "?" else "%.0f°".format(it) }}  ${weatherWord(w.optString("symbol"))}" +
                        (w.optDouble("rain").takeIf { !it.isNaN() && it > 0 }?.let { " · %.1f mm".format(it) } ?: ""))
                    else T(if (!loaded) t("henter …") else if (data == null) t("får ikke kontakt med PC-en") else t("trykk for å velge sted"), color = Z.muted, size = 12)
                    T(t("kilde: MET/Yr"), color = Z.muted, size = 11)
                }
            }
            if (editPlace) {
                Row(Modifier.padding(top = 10.dp), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    val save = {
                        if (place.isNotBlank()) scope.launch {
                            placeMsg = t("slår opp …")
                            runCatching { hub.api.put("/weather/place", JSONObject().put("place", place.trim())) }
                                .onSuccess { editPlace = false; placeMsg = ""; place = ""; data = hub.brief() }
                                .onFailure { placeMsg = it.message ?: "feil" }
                        }
                    }
                    Field(place, { place = it }, t("sted, f.eks. Bergen"), Modifier.weight(1f), onDone = { save() })
                    Btn(t("[ ok ]")) { save() }
                }
                if (placeMsg.isNotEmpty()) T(placeMsg, color = Z.muted, size = 11)
            }
        }
        val s = data?.optJSONObject("server")
        Column(Modifier.rise(1200).fillMaxWidth().border(1.dp, Z.line).padding(12.dp)) {
            T(t("SERVEREN"), color = Z.gold)
            if (s != null) {
                T(t("● olympus kjører"), color = Z.ok)
                T(t("disk %s % · GPU %s · Zeus %s", s.optInt("disk_pct"), s.optDouble("gpu").takeIf { !it.isNaN() }?.let { "%.0f°".format(it) } ?: "?", if (s.optBoolean("ai")) t("på") else t("av")), color = Z.muted)
            } else T(if (loaded) t("○ får ikke kontakt med PC-en – planen over er fra telefonen") else t("henter …"), color = Z.muted)
        }
        hub.events.firstOrNull { it.date == today.toString() && it.time > "%02d:%02d".format(LocalTime.now().hour, LocalTime.now().minute) }?.let { e ->
            T(t("⏰ neste i dag: %s %s", e.time, e.title), Modifier.rise(1500), color = Z.muted, size = 12)
        }
        Spacer(Modifier.weight(1f))
        Row(Modifier.rise(1500), horizontalArrangement = Arrangement.spacedBy(10.dp)) {
            Btn(t("[ til planen ]"), Modifier.weight(1f), solid = true) { onDone("plan") }
            Btn(t("[ spør Zeus ]"), Modifier.weight(1f)) { onDone("zeus") }
        }
    }
}

fun weatherWord(symbol: String): String = when {
    symbol.startsWith("clearsky") -> "klart"
    symbol.startsWith("fair") -> "lettskyet"
    symbol.startsWith("partlycloudy") -> t("delvis skyet")
    symbol.startsWith("cloudy") -> "skyet"
    "thunder" in symbol -> "torden"
    "snow" in symbol -> t("snø")
    "sleet" in symbol -> "sludd"
    "rain" in symbol -> "regn"
    "fog" in symbol -> t("tåke")
    else -> symbol
}

// --- Zeus ----------------------------------------------------------------------------------------
@Composable
fun ZeusTab(hub: Hub) {
    LaunchedEffect(Unit) { hub.warmZeus() }  // fanen åpnet: modellen gjøres klar mens du tenker/skriver
    if (!hub.ai) {
        Column(Modifier.fillMaxSize().padding(20.dp), verticalArrangement = Arrangement.spacedBy(14.dp)) {
            Art(Ascii.ZEUS_OFF, color = Z.muted, size = 11f, glow = false)
            Btn(t("[ slå på Zeus ]"), Modifier.fillMaxWidth(), gold = true) { hub.switchAi(true) }
            T(t("Med Zeus av frigjøres GPU-en på PC-en."), color = Z.muted, size = 11)
        }
        return
    }
    var input by remember { mutableStateOf("") }
    LaunchedEffect(hub.draft) { if (hub.draft.isNotEmpty()) { input = hub.draft; hub.draft = "" } }
    val speech = androidx.activity.compose.rememberLauncherForActivityResult(
        androidx.activity.result.contract.ActivityResultContracts.StartActivityForResult()) { r ->
        r.data?.getStringArrayListExtra(android.speech.RecognizerIntent.EXTRA_RESULTS)?.firstOrNull()?.let { input = it; hub.warmZeus() }
    }
    fun listen() = runCatching { speech.launch(android.content.Intent(android.speech.RecognizerIntent.ACTION_RECOGNIZE_SPEECH)
        .putExtra(android.speech.RecognizerIntent.EXTRA_LANGUAGE_MODEL, android.speech.RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
        .putExtra(android.speech.RecognizerIntent.EXTRA_LANGUAGE, "nb-NO")
        .putExtra(android.speech.RecognizerIntent.EXTRA_PROMPT, t("Snakk til Zeus"))) }
        .onFailure { hub.toast = t("Fant ingen talegjenkjenning på telefonen") }
    LaunchedEffect(hub.voice) { if (hub.voice) { hub.voice = false; listen() } }  // fra hurtiginnstillingen
    val list = rememberLazyListState()
    LaunchedEffect(hub.chat.size, (hub.chat.lastOrNull() as? Chat.Zeus)?.text?.length) {
        if (hub.chat.isNotEmpty()) list.animateScrollToItem(hub.chat.size - 1)
    }
    var sub by remember { mutableStateOf("") }
    if (sub == "samtaler") { SessionsView(hub) { sub = "" }; return }
    if (sub == "minne") { MemoryView(hub) { sub = "" }; return }
    Column(Modifier.fillMaxSize()) {
        ModelChip(hub)
        Row(Modifier.padding(horizontal = 8.dp)) {
            Box(Modifier.clickable { sub = "samtaler" }.padding(8.dp)) { T(t("≡ samtaler"), color = Z.muted, size = 12) }
            Box(Modifier.clickable { sub = "minne" }.padding(8.dp)) { T(t("◈ det Zeus husker"), color = Z.muted, size = 12) }
        }
        LazyColumn(Modifier.weight(1f).padding(horizontal = 16.dp), state = list, verticalArrangement = Arrangement.spacedBy(12.dp)) {
            if (hub.chat.isEmpty()) item {
                Column(Modifier.padding(top = 20.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    T(t("Spør Zeus om hva som helst – eller be ham ordne planen:"), color = Z.muted)
                    listOf(t("hva skjer i dag?"), t("legg inn trening i morgen kl 18"), t("hvor mye diskplass har serveren?")).forEach { q ->
                        Box(Modifier.fillMaxWidth().border(1.dp, Z.line).clickable { hub.send(q) }.padding(10.dp)) { T(t("› %s", q), color = Z.muted, size = 12) }
                    }
                }
            }
            items(hub.chat, contentType = { it::class }) { c ->  // samme slags rader gjenbrukes mens svaret strømmer
                when (c) {
                    is Chat.Mine -> Column(Modifier.border(width = 0.dp, color = Z.bg).padding(start = 0.dp).copyOnLongPress(hub, c.text)) {
                        Row { Box(Modifier.width(2.dp).heightIn(min = 36.dp).background(Z.gold)); Spacer(Modifier.width(10.dp))
                            Column { T(t("du ›"), color = Z.muted, size = 11); T(c.text) } }
                    }
                    is Chat.Zeus -> Column(Modifier.copyOnLongPress(hub, c.text)) {
                        Row(verticalAlignment = Alignment.CenterVertically) { T(t("ϟ Zeus"), color = Z.gold, bold = true, glow = true); Spacer(Modifier.width(8.dp)); Rule(Modifier.weight(1f)) }
                        c.tools.forEach { T(t("│ ⚙ %s", it.removePrefix("⚙ ")), color = Z.muted, size = 11, maxLines = 2) }
                        if (c.text.isNotEmpty()) T(c.text) else if (!c.done) T("▌", color = Z.gold)
                    }
                    is Chat.Notice -> Row(verticalAlignment = Alignment.CenterVertically) {
                        T(c.text, Modifier.weight(1f), color = Z.muted, size = 11)
                        c.retry?.let { q -> Box(Modifier.clickable { hub.send(q) }.padding(8.dp)) { T(t("↻ prøv igjen"), color = Z.gold, size = 11) } }
                        if (c.undo != null) Box(Modifier.clickable { hub.undo(c) }.padding(8.dp)) { T(t("↶ angre"), color = Z.gold, size = 11) }
                    }
                    is Chat.Approval -> ApprovalCard(hub, c, Modifier.padding(vertical = 6.dp))
                }
            }
        }
        if (hub.status.isNotEmpty()) T(t("ϟ %s", hub.status), Modifier.padding(horizontal = 16.dp), color = Z.gold, size = 11)
        Row(Modifier.padding(12.dp), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Field(input, { input = it; if (it.isNotEmpty()) hub.warmZeus() }, t("skriv til Zeus …"), Modifier.weight(1f), onDone = { if (hub.send(input)) input = "" })
            if (hub.busy) Btn("■", danger = true) { hub.stopAnswer() }
            // Telefonens egen talegjenkjenning (norsk); teksten havner i feltet så du kan rette før du sender
            else if (input.isBlank()) Btn(t("◉ tal"), gold = true) { listen() }
            else Btn("⏎", gold = true) { if (hub.send(input)) input = "" }
        }
    }
}

// --- plan ----------------------------------------------------------------------------------------
@Composable
fun PlanTab(hub: Hub) {
    var editing by remember { mutableStateOf<Event?>(null) }
    var adding by remember { mutableStateOf(false) }
    var deleting by remember { mutableStateOf<Event?>(null) }
    var week by remember { mutableStateOf(0L) }
    val today = LocalDate.now()
    val monday = today.minusDays((today.dayOfWeek.value - 1).toLong()).plusWeeks(week)
    // Lista viser de neste 31 dagene med avtaler – samme som Lab's på PC-en (hjem og plan sier alltid det samme)
    val start = if (week == 0L) today else monday
    val busy = hub.events.map { it.date }.toSet()
    val shown = (0L until 31L).map { start.plusDays(it) }.filter { it.toString() in busy }
    val waiting = hub.chat.filterIsInstance<Chat.Approval>()
    val list = androidx.compose.foundation.lazy.rememberLazyListState()
    val scope = rememberCoroutineScope()
    // Indeksen til hver dags overskrift i lista (godkjenninger + kalender kommer først), så et trykk i kalenderen kan rulle dit
    val headerIndex = buildMap {
        var i = waiting.size + 1 + if (shown.isEmpty()) 1 else 0
        shown.forEach { d -> put(d, i); i += 1 + hub.events.count { it.date == d.toString() } }
    }
    LazyColumn(Modifier.fillMaxSize().padding(horizontal = 16.dp), state = list, verticalArrangement = Arrangement.spacedBy(4.dp)) {
        // Zeus venter på et ja: kortet står øverst i planen og lyser rødt til du har svart.
        items(waiting, key = { "a-" + it.id }) { c -> ApprovalCard(hub, c, Modifier.padding(vertical = 8.dp)) }
        item {
            Row(Modifier.fillMaxWidth().padding(top = 4.dp), verticalAlignment = Alignment.CenterVertically) {
                Box(Modifier.clickable { week -= 1 }.padding(10.dp)) { T("‹", color = Z.gold) }
                T("${monday.month.getDisplayName(JTextStyle.FULL, NB).uppercase(NB)} ${monday.year} · uke ${monday.get(java.time.temporal.IsoFields.WEEK_OF_WEEK_BASED_YEAR)}",
                    Modifier.weight(1f), color = Z.gold, bold = true, glow = true, align = TextAlign.Center)
                Box(Modifier.clickable { week += 1 }.padding(10.dp)) { T("›", color = Z.gold) }
            }
            val ctx = androidx.compose.ui.platform.LocalContext.current
            val scope = rememberCoroutineScope()
            Box(Modifier.fillMaxWidth().clickable { scope.launch { exportIcs(hub, ctx) } }.padding(vertical = 2.dp)) {
                T(t("⇪ eksporter til kalender (.ics)"), Modifier.align(Alignment.CenterEnd), color = Z.muted, size = 11)
            }
            // Minikalender: to uker, i dag i gull, dager med avtaler merket med ·
            Column(Modifier.fillMaxWidth().padding(bottom = 8.dp)) {
                Row { listOf("ma", "ti", "on", "to", "fr", t("lø"), t("sø")).forEach { T(it, Modifier.weight(1f), color = Z.muted, size = 11, align = TextAlign.Center) } }
                (0L until 2L).forEach { w ->
                    Row {
                        (0L until 7L).forEach { k ->
                            val d = monday.plusWeeks(w).plusDays(k)
                            val on = d == today
                            Box(Modifier.weight(1f).padding(1.dp).background(if (on) Z.gold else Z.bg)
                                .clickable(enabled = d in headerIndex) { headerIndex[d]?.let { scope.launch { list.animateScrollToItem(it) } } }
                                .padding(vertical = 3.dp), contentAlignment = Alignment.Center) {
                                T("${d.dayOfMonth}${if (d.toString() in busy) "·" else ""}", color = if (on) Z.bg else if (d.toString() in busy) Z.gold else Z.text,
                                    size = 12, bold = on)
                            }
                        }
                    }
                }
            }
        }
        if (shown.isEmpty()) item { T(t("ingen avtaler de neste 31 dagene"), Modifier.padding(vertical = 10.dp), color = Z.muted) }
        shown.forEach { d ->
            val evs = hub.events.filter { it.date == d.toString() }
            item {
                val label = d.dayOfWeek.getDisplayName(JTextStyle.FULL, NB).uppercase(NB) + t(" %s", d.dayOfMonth) + if (d == today) t(" · i dag") else ""
                Section(label, Modifier.padding(top = 8.dp))
            }
            items(evs, key = { it.id }) { ev ->
                Row(Modifier.fillMaxWidth().clickable { editing = ev }, verticalAlignment = Alignment.CenterVertically) {
                    Box(Modifier.width(2.dp).heightIn(min = 30.dp).background(if (ev.source == "zeus") Z.gold else Z.line))
                    Spacer(Modifier.width(9.dp))
                    T(ev.time.padEnd(10), color = Z.muted)
                    T(ev.title, Modifier.weight(1f), maxLines = 1)
                    if (ev.source == "zeus") T("ϟ ", color = Z.gold, glow = true)
                    if (ev.series.isNotEmpty()) T("↻", color = Z.muted, size = 12)
                    Box(Modifier.clickable { if (ev.series.isNotEmpty()) deleting = ev else hub.deleteEvent(ev) }.padding(10.dp)) {
                        T("✕", color = Z.muted, size = 12)
                    }
                }
            }
        }
        item {
            Btn(t("[ + ny avtale ]"), Modifier.fillMaxWidth().padding(vertical = 14.dp), gold = true) { adding = true }
        }
    }
    if (adding) EventDialog(null, { adding = false }) { t, s, n, r -> hub.addEvent(t, s, n, r); adding = false }
    editing?.let { ev -> EventDialog(ev, { editing = null }) { t, s, n, _ -> hub.editEvent(ev, t, s, n); editing = null } }
    deleting?.let { ev ->
        androidx.compose.ui.window.Dialog(onDismissRequest = { deleting = null }) {
            Column(Modifier.fillMaxWidth().background(Z.bg).border(1.dp, Z.gold).padding(16.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
                T(t("↻ SLETTE «%s»?", ev.title), color = Z.gold, bold = true, glow = true, maxLines = 2)
                T(t("Avtalen gjentas. Begge kan angres."), color = Z.muted, size = 12)
                Btn(t("[ bare denne ]"), Modifier.fillMaxWidth()) { hub.deleteEvent(ev); deleting = null }
                Btn(t("[ denne og alle senere ]"), Modifier.fillMaxWidth(), danger = true) { hub.deleteEvent(ev, rest = true); deleting = null }
                Btn(t("[ avbryt ]"), Modifier.fillMaxWidth()) { deleting = null }
            }
        }
    }
}

/** «ϟ ZEUS VIL …»-kortet: samme i chatten og i planen. Farlige endringer lyser rødt. */
@Composable
fun ApprovalCard(hub: Hub, c: Chat.Approval, modifier: Modifier = Modifier) {
    val col = if (c.danger) Z.bad else Z.gold
    Column(modifier.fillMaxWidth().alarm(col).border(1.dp, col).background(Z.surface).padding(12.dp),
        verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            T(t("ϟ ZEUS VIL"), color = col, bold = true, glow = true); Spacer(Modifier.width(8.dp)); Rule(Modifier.weight(1f), color = col.copy(alpha = 0.35f))
        }
        T(c.reason, color = Z.muted, size = 12)
        T(c.preview)
        Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
            Btn(t("[ godkjenn ]"), Modifier.weight(1f), solid = true) { hub.answer(c, true) }
            Btn(t("[ avslå ]"), Modifier.weight(1f)) { hub.answer(c, false) }
        }
        // Som «a» i Lab's: samme verktøy spør ikke igjen i denne samtalen. Aldri for røde (farlige) kall.
        if (!c.danger) Box(Modifier.fillMaxWidth().clickable { hub.answer(c, true, always = true) }.padding(vertical = 4.dp)) {
            T(t("› godkjenn alltid i denne samtalen"), color = Z.muted, size = 11)
        }
    }
}

private val REPEATS = listOf("nei" to "", "dag" to "daily", "man–fre" to "weekdays", "uke" to "weekly", "måned" to "monthly")

@Composable
fun EventDialog(ev: Event?, onClose: () -> Unit, onSave: (String, String, String, String) -> Unit) {
    var repeat by remember { mutableStateOf("nei") }
    var title by remember { mutableStateOf(ev?.title.orEmpty()) }
    var date by remember { mutableStateOf(ev?.date ?: LocalDate.now().toString()) }
    var time by remember { mutableStateOf(if (ev == null) "%02d:00".format((LocalTime.now().hour + 1) % 24) else if (ev.allDay) "" else ev.time) }
    var note by remember { mutableStateOf(ev?.note.orEmpty()) }
    var error by remember { mutableStateOf<String?>(null) }
    androidx.compose.ui.window.Dialog(onDismissRequest = onClose) {
        Column(Modifier.fillMaxWidth().background(Z.bg).border(1.dp, Z.gold).padding(16.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
            T(if (ev == null) t("▦ NY AVTALE") else t("▦ ENDRE AVTALE"), color = Z.gold, bold = true, glow = true)
            Field(title, { title = it }, "hva", Modifier.fillMaxWidth())
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Field(date, { date = it }, t("ÅÅÅÅ-MM-DD"), Modifier.weight(1.4f))
                Field(time, { time = it }, t("tt:mm (tom = hele dagen)"), Modifier.weight(1f))
            }
            Field(note, { note = it }, t("notat (valgfritt)"), Modifier.fillMaxWidth())
            if (ev == null) {
                T(t("gjentas"), color = Z.muted, size = 11)
                Seg(REPEATS.map { it.first }, repeat, inset = 0.dp) { repeat = it }
                if (repeat != "nei") T(t("↻ legges inn %s fram – kan slettes fra hvilken som helst dag", if (repeat == "måned") "ett år" else "tre måneder"), color = Z.muted, size = 11)
            } else if (ev.series.isNotEmpty()) T(t("↻ del av en gjentatt avtale – endringen gjelder bare denne"), color = Z.muted, size = 11)
            error?.let { T(t("✗ %s", it), color = Z.bad, size = 12) }
            Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                Btn(t("[ lagre ]"), Modifier.weight(1f), solid = true) {
                    val ok = runCatching { LocalDate.parse(date.trim()); if (time.isNotBlank()) LocalTime.parse(time.trim()) }.isSuccess
                    when {
                        title.isBlank() -> error = t("Skriv hva avtalen er.")
                        !ok -> error = t("Dato som 2026-10-14 og tid som 14:00.")
                        else -> onSave(title.trim(), if (time.isBlank()) date.trim() else "${date.trim()}T${time.trim()}", note.trim(),
                            REPEATS.first { it.first == repeat }.second)
                    }
                }
                Btn(t("[ avbryt ]"), Modifier.weight(1f)) { onClose() }
            }
        }
    }
}

/** Hvilken modell Zeus svarer med. Hjernen (minne, verktøy, plan) blir alltid på PC-en – bare modellen byttes.
 *  Skymodeller merkes ☁: da sendes samtalen til den leverandøren. */
@Composable
fun ModelChip(hub: Hub) {
    val scope = rememberCoroutineScope()
    var data by remember { mutableStateOf<JSONObject?>(null) }
    var open by remember { mutableStateOf(false) }
    LaunchedEffect(hub.modelVersion, open) { data = runCatching { hub.api.get("/models") }.getOrNull() }
    val d = data ?: return
    val choices = d.optJSONArray("choices").list { it }
    val cur = choices.firstOrNull { it.optString("id") == d.optString("current") }
    val cloud = cur?.optBoolean("cloud") == true
    // Navnet alene på første linje (uten leverandørprefiks, kuttes heller enn å presse sky-varselet i tre linjer);
    // skyvarselet med leverandøren på en egen, kort linje under. Lokal er normalen: ingen ekstra merking.
    val name = (cur?.optString("label") ?: d.optString("current")).substringAfter(": ").removeSuffix(" · lokal")
    Column(Modifier.padding(horizontal = 16.dp, vertical = 2.dp).fillMaxWidth().clickable { open = true }.padding(vertical = 6.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            T(t("modell: "), color = Z.muted, size = 11)
            T(name, Modifier.weight(1f, fill = false), color = if (cloud) Z.bad else Z.gold, size = 11, maxLines = 1)
            T(" ▾", color = if (cloud) Z.bad else Z.gold, size = 11)
        }
        if (cloud) T(t("☁ %s · sky – samtalen sendes ut", cur?.optString("provider")), color = Z.bad, size = 10, maxLines = 1)
    }
    if (open) androidx.compose.ui.window.Dialog(onDismissRequest = { open = false }) {
        // Overskriften står fast; listen ruller (før ble alt under ~15 modeller kuttet: Gemini og Claude Code manglet).
        // Én seksjon per leverandør, så navnene slipper prefikset og får plass.
        Column(Modifier.fillMaxWidth().heightIn(max = 640.dp).background(Z.bg).border(1.dp, Z.gold).padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(6.dp)) {
            T(t("ϟ HVEM SKAL SVARE?"), color = Z.gold, bold = true, glow = true)
            T(t("Zeus' hjerne blir på PC-en. Velger du en skymodell, sendes samtalen dit."), color = Z.muted, size = 11)
            Column(Modifier.weight(1f, fill = false).verticalScroll(rememberScrollState())) {
                choices.groupBy { if (it.optBoolean("cloud")) it.optString("provider") else "" }.forEach { (prov, group) ->
                    val sky = prov.isNotEmpty()
                    val cost = group.first().optString("cost")
                    Section(if (sky) "$prov · ${cost.uppercase()}" else "LOKALT", Modifier.padding(top = 6.dp))
                    group.forEach { c ->
                        val on = c.optString("id") == d.optString("current")
                        val name = c.optString("label").substringAfter(": ").removeSuffix(" · lokal")
                        Row(Modifier.fillMaxWidth().clickable {
                            open = false
                            scope.launch {
                                runCatching { hub.api.post("/models", JSONObject().put("model", c.optString("id"))) }
                                    .onSuccess { hub.toast = t("Zeus svarer nå med %s", c.optString("label")) + if (sky) t(" (☁ sky)") else "" }
                                    .onFailure { hub.toast = t("Fikk ikke byttet modell: %s", it.message) }
                                data = runCatching { hub.api.get("/models") }.getOrNull()
                            }
                        }.padding(vertical = 8.dp), verticalAlignment = Alignment.CenterVertically) {
                            T(if (on) "● " else "○ ", color = if (on) Z.gold else Z.muted)
                            T(name, Modifier.weight(1f), color = if (on) Z.gold else Z.text, maxLines = 2)
                            c.optString("size").takeIf { it.isNotEmpty() && it != "null" }?.let { T(it, color = Z.muted, size = 11) }
                        }
                    }
                }
            }
        }
    }
}


/** Langt trykk kopierer teksten (svar fra Zeus eller din egen melding). */
@Composable
fun Modifier.copyOnLongPress(hub: Hub, text: String): Modifier {
    val clip = androidx.compose.ui.platform.LocalClipboardManager.current
    return this.pointerInput(text) {
        detectTapGestures(onLongPress = {
            if (text.isNotBlank()) { clip.setText(androidx.compose.ui.text.AnnotatedString(text)); hub.toast = "Kopiert" }
        })
    }
}


/** Planen som .ics til en annen kalender: åpnes med kalenderappen (import) eller deles som fil. */
suspend fun exportIcs(hub: Hub, ctx: android.content.Context) {
    runCatching {
        val file = kotlinx.coroutines.withContext(kotlinx.coroutines.Dispatchers.IO) {
            val text = hub.api.stream("/plan/export.ics", null).use { r ->
                if (!r.isSuccessful) throw java.io.IOException("HTTP ${r.code}"); r.body?.string().orEmpty() }
            java.io.File(ctx.cacheDir, "export").apply { mkdirs() }.resolve("zeus-plan.ics").apply { writeText(text) }
        }
        val uri = androidx.core.content.FileProvider.getUriForFile(ctx, ctx.packageName + ".updates", file)
        val view = android.content.Intent(android.content.Intent.ACTION_VIEW).setDataAndType(uri, "text/calendar")
            .addFlags(android.content.Intent.FLAG_GRANT_READ_URI_PERMISSION)
        val send = android.content.Intent(android.content.Intent.ACTION_SEND).setType("text/calendar")
            .putExtra(android.content.Intent.EXTRA_STREAM, uri).addFlags(android.content.Intent.FLAG_GRANT_READ_URI_PERMISSION)
        ctx.startActivity(android.content.Intent.createChooser(view, t("Planen til kalender"))
            .putExtra(android.content.Intent.EXTRA_INITIAL_INTENTS, arrayOf(send))
            .addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK))
    }.onFailure { hub.toast = t("Fikk ikke eksportert planen: %s", it.message) }
}
