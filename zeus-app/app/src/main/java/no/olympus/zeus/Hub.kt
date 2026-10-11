package no.olympus.zeus

import android.content.Context
import android.os.Build
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONObject
import java.time.LocalDate
import java.util.UUID

sealed class Chat {
    data class Mine(val text: String) : Chat()
    data class Zeus(val text: String, val tools: List<String>, val done: Boolean) : Chat()
    // retry: meldingen som kan sendes på nytt; undo: endringen «↶ angre» angrer (gjort uten kort)
    data class Notice(val text: String, val retry: String? = null, val undo: Int? = null) : Chat()
    data class Approval(val id: String, val reason: String, val preview: String, val danger: Boolean) : Chat()
}

enum class Sync { OK, BUSY, OFFLINE }

/** Telefonens side av Olympus: planen (lokal kopi + synk), samtalen med Zeus og AI-bryteren.
 *  Alt du gjør selv virker uten AI og uten nett; endringer sendes når PC-en kan nås. */
class Hub(private val context: Context) {
    val store = Store(context)
    val api = Api(store)
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main)

    var events by mutableStateOf(store.events.filter { !it.deleted })
    var sync by mutableStateOf(if (store.pending.isEmpty()) Sync.OK else Sync.OFFLINE)
    var ai by mutableStateOf(true)
    var busy by mutableStateOf(false)
    var status by mutableStateOf("")
    var toast by mutableStateOf<String?>(null)
    var undoSeq by mutableStateOf<Int?>(null)
    val chat = mutableStateListOf<Chat>()
    var modelVersion by mutableStateOf(0)  // modellvalget endret (modellvelgeren henter på nytt)
    var metaVersion by mutableStateOf(0)   // minne/samtaler endret (de åpne listene henter på nytt)
    var mediaVersion by mutableStateOf(0)  // økes når biblioteket/spillelister/lyd endres → fanene henter på nytt
    var planVersion by mutableStateOf(0)
    var update by mutableStateOf<JSONObject?>(null)  // ny app-versjon klar på PC-en
    var navTo by mutableStateOf<String?>(null)  // en widget ba om en fane
    var pc by mutableStateOf<JSONObject?>(null)       // det PC-en spiller (én felles avspiller med telefonen)
    var pcAt by mutableStateOf(0L)                    // når pc ble hentet (for å regne posisjonen videre lokalt)
    var downloads by mutableStateOf(emptyList<JSONObject>())  // nedlastinger på PC-en, live
    var sharedUrl by mutableStateOf<String?>(null)    // lenke delt fra en annen app → «+ legg til»
    var background = false        // appen er ikke synlig: svar/godkjenninger blir varsler
    var stopWhenDone = false      // appen ble lukket mens Zeus svarte: hold forbindelsen til svaret er ferdig
    var voice by mutableStateOf(false)      // hurtiginnstillingen ba om taleinput (Zeus-fanen starter den)
    var draft by mutableStateOf("")       // tekst som skal stå klar i Zeus-feltet (f.eks. «spør om boka»)

    private var ws: WebSocket? = null
    private var wsJob: Job? = null

    // --- tilkobling --------------------------------------------------------------------------------
    fun start() {
        if (!store.paired) return
        scope.launch { refresh() }
        scope.launch {  // PC-widgeten: status hvert 5. minutt mens appen kjører (også i bakgrunnen)
            while (isActive) {
                val d = runCatching { api.get("/dashboard") }.getOrNull()
                Widgets.pc(context, d, downloads)
                delay(2 * 60_000L)
            }
        }
        connect()
    }

    fun stop() {
        wsJob?.cancel()
        ws?.close(1000, null)
        ws = null
    }

    private fun connect() {
        wsJob?.cancel()
        wsJob = scope.launch {
            var backoff = 1000L
            while (true) {
                val closed = kotlinx.coroutines.CompletableDeferred<Unit>()
                ws = api.connect(object : WebSocketListener() {
                    override fun onOpen(webSocket: WebSocket, response: Response) {
                        scope.launch { backoff = 1000L; flushPending(); refresh(); flushQueued(webSocket) }
                    }

                    override fun onMessage(webSocket: WebSocket, text: String) {
                        scope.launch { runCatching { onServer(JSONObject(text)) } }
                    }

                    override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                        if (code == 4001) scope.launch { unpaired() }
                        closed.complete(Unit)
                    }

                    override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                        if (response?.code == 401) scope.launch { unpaired() }
                        scope.launch { sync = Sync.OFFLINE }
                        closed.complete(Unit)
                    }
                })
                closed.await()
                delay(backoff)
                backoff = (backoff * 2).coerceAtMost(30_000L)
            }
        }
    }

    private fun unpaired() {
        stop()
        store.unpair()
        toast = t("Telefonen er fjernet fra Olympus. Par den på nytt med `olympus pair`.")
        onUnpaired?.invoke()
    }

    var onUnpaired: (() -> Unit)? = null
    var onHandoff: ((List<Track>, Int, Long) -> Unit)? = null   // Zeus flyttet avspillingen fra PC-en hit
    var onPaired: (() -> Unit)? = null

    suspend fun pair(link: String): String? {
        val uri = android.net.Uri.parse(link.trim())
        val host = uri.getQueryParameter("h").orEmpty()
        val code = uri.getQueryParameter("c").orEmpty()
        if (uri.scheme != "zeus" || host.isEmpty() || code.isEmpty()) return t("Det er ikke en paringskode fra `olympus pair`.")
        if (!isTailscale(host)) return t("Adressen %s er ikke i Tailscale-nettet ditt. Zeus kobler bare til Olympus over Tailscale.", host)
        return try {
            val r = api.pair(host, code, Build.MODEL ?: "telefon")
            store.host = host
            store.token = r.getString("token")
            store.serverName = r.optString("server", uri.getQueryParameter("n").orEmpty())
            start()
            onPaired?.invoke()
            null
        } catch (e: ApiError) {
            e.message
        } catch (e: Exception) {
            t("Fikk ikke kontakt med %s. Er Tailscale på, og kjører Olympus på PC-en?", host)
        }
    }

    // --- planen ------------------------------------------------------------------------------------
    suspend fun refresh() {
        scope.launch { refreshPc() }
        try {
            val from = LocalDate.now().minusDays(7).toString()
            val r = api.get("/plan?from=$from")
            val arr = r.getJSONArray("events")
            val list = (0 until arr.length()).map { Event.from(arr.getJSONObject(it)) }
            applyEvents(list)
            store.seq = r.optInt("seq")
            val st = api.get("/status")
            ai = st.optBoolean("ai", true)
            if (store.pending.isEmpty()) sync = Sync.OK
        } catch (e: ApiError) {
            if (e.code == 401) unpaired()
        } catch (e: Exception) {
            sync = Sync.OFFLINE
        }
    }

    private fun applyEvents(list: List<Event>) {
        // distinctBy: tmp-avtalen og den ekte kan møtes hvis synken kommer før svaret (samme id to ganger krasjer lista)
        events = list.filter { !it.deleted }.distinctBy { it.id }.sortedBy { it.start }
        store.events = events
        Reminders.schedule(context, events)
    }

    private suspend fun catchUp() {
        try {
            val r = api.get("/plan/changes?since=${store.seq}")
            val arr = r.getJSONArray("events")
            val changed = (0 until arr.length()).map { Event.from(arr.getJSONObject(it)) }
            val byId = events.associateBy { it.id }.toMutableMap()
            changed.forEach { if (it.deleted) byId.remove(it.id) else byId[it.id] = it }
            applyEvents(byId.values.toList())
            store.seq = r.optInt("seq", store.seq)
        } catch (_: Exception) {
            sync = Sync.OFFLINE
        }
    }

    /** Gjør endringen lokalt med en gang, send den til PC-en (eller legg den i kø uten nett). */
    private fun change(op: JSONObject, local: (List<Event>) -> List<Event>, message: String) {
        applyEvents(local(events))
        toast = message
        undoSeq = null
        store.pending = store.pending + op
        sync = Sync.BUSY
        scope.launch { flushPending() }
    }

    private suspend fun flushPending() {
        var queue = store.pending
        while (queue.isNotEmpty()) {
            val op = queue.first()
            try {
                val res = when (op.getString("op")) {
                    "add" -> api.post("/plan", op.getJSONObject("body"))
                    "edit" -> api.patch("/plan/${op.getString("id")}", op.getJSONObject("body"))
                    "delete" -> api.delete("/plan/${op.getString("id")}" + if (op.optBoolean("rest")) "?series=rest" else "")
                    else -> null
                }
                if (op.getString("op") == "add" && res != null) {
                    // Den lokale midlertidige id-en byttes mot serverens.
                    val tmp = op.getString("tmp")
                    val real = Event.from(res)
                    applyEvents(events.filter { it.id != real.id }.map { if (it.id == tmp) real else it })
                    queue = queue.drop(1).map { o -> if (o.optString("id") == tmp) JSONObject(o.toString()).put("id", res.getString("id")) else o }
                } else {
                    queue = queue.drop(1)
                }
                store.pending = queue
            } catch (e: ApiError) {
                if (e.code == 401) { unpaired(); return }
                queue = queue.drop(1) // avvist av serveren (f.eks. allerede slettet): hopp over
                store.pending = queue
            } catch (e: Exception) {
                sync = Sync.OFFLINE
                return
            }
        }
        sync = Sync.OK
        val log = runCatching { api.get("/log") }.getOrNull()
        undoSeq = log?.optJSONArray("items")?.optJSONObject(0)?.optInt("seq")
    }

    /** repeat = "" (én gang), daily, weekdays, weekly, biweekly eller monthly – PC-en lager hele serien,
     *  og telefonen får resten av forekomstene via synken. */
    fun addEvent(title: String, start: String, note: String, repeat: String = "") {
        val tmp = "tmp-" + UUID.randomUUID().toString().take(8)
        // client = tmp: går svaret tapt og køen sender på nytt, kjenner PC-en den igjen og lager ingen kopi
        val body = JSONObject().put("title", title).put("start", start).put("note", note).put("client", tmp)
        if (repeat.isNotEmpty()) body.put("repeat", repeat)
        val ev = Event(tmp, title, start, null, start.length == 10, note, "du")
        change(JSONObject().put("op", "add").put("tmp", tmp).put("body", body), { it + ev }, t("La til «%s»", title))
    }

    fun editEvent(ev: Event, title: String, start: String, note: String) {
        val body = JSONObject().put("title", title).put("start", start).put("note", note)
        change(JSONObject().put("op", "edit").put("id", ev.id).put("body", body),
            { l -> l.map { if (it.id == ev.id) it.copy(title = title, start = start, note = note, allDay = start.length == 10) else it } },
            t("Endret «%s»", title))
    }

    /** rest = også resten av serien (de som kommer etter denne). */
    fun deleteEvent(ev: Event, rest: Boolean = false) {
        val gone: (Event) -> Boolean = { it.id == ev.id || (rest && ev.series.isNotEmpty() && it.series == ev.series && it.start >= ev.start) }
        change(JSONObject().put("op", "delete").put("id", ev.id).put("rest", rest), { l -> l.filterNot(gone) },
            if (rest) t("Slettet resten av «%s»", ev.title) else t("Slettet «%s»", ev.title))
    }

    fun undo() {
        val seq = undoSeq ?: return
        undoSeq = null
        scope.launch {
            runCatching { api.post("/undo/$seq") }.onSuccess { toast = "Angret"; catchUp() }.onFailure { toast = t("Kunne ikke angre nå") }
        }
    }

    // --- Zeus ---------------------------------------------------------------------------------------
    private var lastWarm = 0L

    /** Forvarming: Zeus-fanen ble åpnet eller du begynte å skrive – PC-en laster modellen og regner ut den faste
     *  starten nå, så svaret kommer på under et sekund i stedet for ~11. Høyst én gang i minuttet. */
    fun warmZeus() {
        val now = System.currentTimeMillis()
        if (!ai || now - lastWarm < 60_000) return
        lastWarm = now
        scope.launch { runCatching { api.post("/zeus/warm") } }
    }
    // --- PC-en: felles avspiller og fjernkontroll ---------------------------------------------------
    suspend fun refreshDownloads() {
        runCatching { api.get("/downloads") }.onSuccess { downloads = it.optJSONArray("items").list { x -> x } }
    }

    suspend fun refreshPc() {
        runCatching { api.get("/pc") }.onSuccess { pc = it; pcAt = System.currentTimeMillis() }
    }

    /** Sekunder inn i sangen PC-en spiller nå (regnet videre fra siste melding, så linja går jevnt). */
    fun pcPosition(): Double {
        val p = pc ?: return 0.0
        val base = p.optDouble("position", 0.0).takeIf { !it.isNaN() } ?: 0.0
        val running = p.optBoolean("playing")  // false både ved pause og stopp
        return base + if (running) (System.currentTimeMillis() - pcAt) / 1000.0 else 0.0
    }

    fun pcControl(action: String) = scope.launch {
        runCatching { api.post("/pc/control", JSONObject().put("action", action)) }
            .onSuccess { pc = it; pcAt = System.currentTimeMillis() }.onFailure { toast = t("Fikk ikke styrt PC-en") }
    }

    /** Fra telefonen til PC-en: samme kø fra samme sted. */
    fun moveToPc(ids: List<String>, positionSec: Double, after: () -> Unit) = scope.launch {
        runCatching { api.post("/pc/play", JSONObject().put("tracks", org.json.JSONArray(ids)).put("index", 0).put("position", positionSec)) }
            .onSuccess { pc = it; pcAt = System.currentTimeMillis(); toast = t("Spiller videre på PC-en"); after() }
            .onFailure { toast = t("Fikk ikke flyttet til PC-en") }
    }

    /** Fra PC-en til telefonen: PC-en stopper og gir fra seg køen og stedet. */
    fun moveToPhone(play: (List<Track>, Int, Long) -> Unit) = scope.launch {
        runCatching { api.post("/pc/handoff") }.onSuccess { r ->
            val tracks = r.optJSONArray("tracks").list(Track::from)
            if (tracks.isNotEmpty()) play(tracks, r.optInt("index"), (r.optDouble("position", 0.0) * 1000).toLong())
            refreshPc(); toast = t("Spiller videre på telefonen")
        }.onFailure { toast = t("PC-en spiller ingenting å flytte") }
    }

    fun pcAction(action: String, confirm: Boolean = false) = scope.launch {
        runCatching { api.post("/pc/action", JSONObject().put("action", action).put("confirm", confirm)) }
            .onSuccess { toast = mapOf("lock" to t("PC-en er låst"), "screen_off" to t("Skjermen er av"), "screen_on" to t("Skjermen er på"),
                "sleep" to t("PC-en sover – Olympus er borte til den våkner"), "open_olympus" to t("Olympus er åpnet på PC-en"))[action] ?: "OK" }
            .onFailure { toast = t("PC-en svarte ikke (%s)", it.message) }
    }

    fun switchAi(on: Boolean) {
        ai = on
        scope.launch {
            runCatching { api.post("/ai", JSONObject().put("on", on)) }
                .onFailure { toast = t("Fikk ikke kontakt med PC-en") }
        }
        toast = if (on) t("Zeus er på") else t("Zeus er av · alt annet virker og synkes som før")
    }

    private var lastSent: String? = null
    private var queued: Pair<String, Long>? = null  // skrevet uten kontakt med PC-en: sendes når den svarer igjen

    /** Sender meldingen. Uten kontakt med PC-en legges den i kø og sendes av seg selv når forbindelsen er tilbake
     *  (innen 10 min – et gammelt spørsmål skal ikke dukke opp timer senere). */
    fun send(text: String): Boolean {
        val t = text.trim()
        if (t.isEmpty()) return false
        val sock = ws
        if (sock == null || sync == Sync.OFFLINE || !sock.send(JSONObject().put("type", "send").put("text", t).toString())) {
            queued = t to System.currentTimeMillis()
            chat.add(Chat.Notice(t("⧗ i kø – sendes når PC-en svarer: %s", t.take(60))))
            return true
        }
        lastSent = t
        return true
    }

    private fun flushQueued(sock: WebSocket) {
        val (t, at) = queued ?: return
        queued = null
        if (System.currentTimeMillis() - at > 10 * 60_000) { chat.add(Chat.Notice(t("✗ ikke sendt (for gammel): %s", t.take(60)))); return }
        if (sock.send(JSONObject().put("type", "send").put("text", t).toString())) lastSent = t
    }

    fun undo(n: Chat.Notice) {
        val seq = n.undo ?: return
        scope.launch {
            runCatching { api.post("/undo/$seq") }
                .onSuccess { val i = chat.indexOf(n); if (i >= 0) chat[i] = Chat.Notice(t("↶ angret: ") + n.text.removePrefix("✓ ")) }
                .onFailure { toast = t("Fikk ikke angret: %s", it.message) }
        }
    }

    /** always: godkjent for resten av samtalen (samme verktøy spør ikke igjen). Serveren avviser det for farlige kall. */
    fun answer(a: Chat.Approval, yes: Boolean, always: Boolean = false) {
        val ans = if (!yes) "no" else if (always && !a.danger) "always" else "yes"
        ws?.send(JSONObject().put("type", "approve").put("id", a.id).put("answer", ans).toString())
        chat.remove(a)
        chat.add(Chat.Notice(when (ans) { "always" -> t("✓ godkjent – spør ikke igjen i denne samtalen"); "yes" -> t("✓ godkjent"); else -> t("✗ avslått") }))
    }

    fun stopAnswer() {
        ws?.send(JSONObject().put("type", "interrupt").toString())
    }

    private var current: Chat.Zeus? = null

    private fun updateZeus(transform: (Chat.Zeus) -> Chat.Zeus) {
        val z = current ?: Chat.Zeus("", emptyList(), false).also { chat.add(it) }
        val i = chat.indexOf(z)
        val n = transform(z)
        current = n
        if (i >= 0) chat[i] = n else chat.add(n)
    }

    private suspend fun onServer(m: JSONObject) {
        when (m.optString("type")) {
            "plan_changed" -> { if (m.optInt("seq") > store.seq) catchUp(); planVersion++ }
            // Hver sin teller: før lastet et modellbytte eller et nytt minne alle mediefanene og PC-en på nytt
            "media_changed" -> {
                if (m.optString("what") == "cover") Images.bust()
                mediaVersion++
            }
            "model" -> modelVersion++
            "memory_changed", "sessions_changed" -> metaVersion++
            "handoff" -> onHandoff?.invoke(m.optJSONArray("tracks").list(Track::from), m.optInt("index"),
                (m.optDouble("position", 0.0) * 1000).toLong())
            "player" -> { pc = m; pcAt = System.currentTimeMillis() }
            "downloads" -> { downloads = m.optJSONArray("items").list { it }; Widgets.downloads(context, downloads) }
            "ai" -> ai = m.optBoolean("on", true)
            "state" -> ai = m.optJSONObject("state")?.optBoolean("ai", ai) ?: ai
            "user" -> { chat.add(Chat.Mine(m.optString("text"))); current = null; busy = true }
            "status" -> status = m.optString("text")
            "agent_start" -> { current = null; updateZeus { it } }
            "chunk" -> updateZeus { it.copy(text = it.text + m.optString("text")) }
            "tool" -> updateZeus { it.copy(tools = it.tools + t("⚙ %s %s", m.optString("name"), m.optString("detail")).trim()) }
            "tool_result" -> if (!m.optBoolean("ok")) updateZeus { it.copy(tools = it.tools + t("✗ %s", m.optString("summary"))) }
            "approval" -> {
                chat.add(Chat.Approval(m.optString("id"), m.optString("reason"), m.optString("preview"), m.optBoolean("danger")))
                if (background) Notify.approval(context, m.optString("reason"))
            }
            "approval_done" -> chat.removeAll { it is Chat.Approval && it.id == m.optString("id") }
            "notice" -> chat.add(Chat.Notice(m.optString("text")))
            "undoable" -> chat.add(Chat.Notice("✓ " + m.optString("text"), undo = m.optInt("seq")))
            "error" -> chat.add(Chat.Notice(m.optString("text"), retry = lastSent))
            "judge_rejected" -> { // svaret ble forkastet (f.eks. «lagt inn» uten verktøy): vis bare det nye
                current?.let { z -> chat.remove(z) }
                current = null
                chat.add(Chat.Notice(t("Første svar forkastet: %s", m.optString("text"))))
            }
            "done" -> {
                busy = false; status = ""
                current?.let { z -> updateZeus { z.copy(done = true) }; if (background && z.text.isNotBlank()) Notify.answer(context, z.text) }
                current = null
                if (background && stopWhenDone) { stopWhenDone = false; stop(); AnswerService.stop(context) }  // svaret er levert
            }
            "history" -> {
                chat.clear()
                val items = m.optJSONArray("items") ?: return
                for (i in 0 until items.length()) {
                    val it = items.getJSONObject(i)
                    chat.add(if (it.optString("kind") == "user") Chat.Mine(it.optString("text"))
                             else Chat.Zeus(it.optString("text"), it.optJSONArray("tools")?.let { a -> (0 until a.length()).map { k -> a.getString(k) } } ?: emptyList(), true))
                }
            }
            "cleared" -> chat.clear()
        }
    }

    fun newChat() {
        ws?.send(JSONObject().put("type", "new").toString())
    }

    companion object {
        /** Bare en Tailscale-IP (100.64.0.0/10) med valgfri port, skrevet nøyaktig slik – ingen vertsnavn (også
         *  *.ts.net kan gjøres offentlig med Funnel), ingen «@», «/» eller annet som kan lure URL-tolkningen. */
        private val TAILSCALE = Regex("""^100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.(\d{1,3})\.(\d{1,3})(:(\d{1,5}))?$""")

        fun isTailscale(hostPort: String): Boolean {
            val m = TAILSCALE.matchEntire(hostPort) ?: return false
            val octets = listOf(m.groupValues[2], m.groupValues[3]).map { it.toInt() }
            val port = m.groupValues[5].toIntOrNull() ?: 80
            return octets.all { it in 0..255 } && port in 1..65535
        }

        fun hostOf(link: String): String = android.net.Uri.parse(link.trim()).getQueryParameter("h").orEmpty()
    }

    suspend fun brief(): JSONObject? = runCatching { api.get("/brief") }.getOrNull()

    val briefDue: Boolean get() = store.lastBrief != LocalDate.now().toString()
    fun briefSeen() { store.lastBrief = LocalDate.now().toString() }
}
