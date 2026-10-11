package no.olympus.zeus

import android.app.AlarmManager
import android.app.PendingIntent
import android.appwidget.AppWidgetManager
import android.appwidget.AppWidgetProvider
import android.content.BroadcastReceiver
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.widget.RemoteViews
import org.json.JSONArray
import org.json.JSONObject
import java.time.LocalDate
import java.time.LocalTime
import java.time.format.TextStyle
import java.util.Locale

/* Widgets på hjemskjermen i terminalstil (tegnet av WidgetArt, som utkastet):
 *   ϟ Zeus (4×4): klokke, PC-en og nedlastinger · ϟ Klokke · ◈ PC-en · ↓ Nedlastinger
 * ASCII-sifre med skygge, gull hjørner, segmentmålere. Klokka tegnes hvert minutt (alarm som ikke vekker telefonen);
 * PC-en og nedlastingene oppdateres mens appen kjører (også i bakgrunnen) og viser siste kjente tilstand ellers.
 * Et trykk åpner riktig fane i appen. */
object Widgets {
    private const val PREFS = "widgets"
    private val NB = Locale.forLanguageTag("nb")
    private fun prefs(ctx: Context) = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
    private var lastDl = 0L

    /** PC-en fra /dashboard: temperaturer, disk, nettverk. null = ingen kontakt. */
    fun pc(ctx: Context, d: JSONObject?, downloads: List<JSONObject>) {
        val s = d?.optJSONObject("server")
        val net = s?.optJSONObject("net")
        prefs(ctx).edit().putBoolean("pc_online", d != null)
            .putFloat("pc_gpu", s?.optDouble("gpu")?.toFloat()?.takeIf { !it.isNaN() } ?: -1f)
            .putFloat("pc_cpu", s?.optDouble("cpu")?.toFloat()?.takeIf { !it.isNaN() } ?: -1f)
            .putInt("pc_disk", s?.optInt("disk_pct", -1) ?: -1)
            .putLong("net_down", net?.optLong("down", -1) ?: -1).putLong("net_up", net?.optLong("up", -1) ?: -1)
            .putLong("pc_at", System.currentTimeMillis()).apply()
        this.downloads(ctx, downloads, force = true)
    }

    /** Nedlastingene som pågår (fra websocket-hendelsen «downloads»). Tegnes høyst hvert 10. sekund. */
    fun downloads(ctx: Context, list: List<JSONObject>, force: Boolean = false) {
        val arr = JSONArray()
        list.filter { it.optString("status") == "laster ned" }.take(3).forEach {
            arr.put(JSONObject().put("t", it.optString("title")).put("p", it.optDouble("progress", 0.0))
                .put("k", it.optString("kind")).put("m", it.optString("method")).put("r", it.optLong("rate", 0)))
        }
        val p = prefs(ctx)
        if (!force && p.getString("dl", "[]") == arr.toString()) return
        p.edit().putString("dl", arr.toString()).apply()
        val now = System.currentTimeMillis()
        if (force || now - lastDl > 10_000) { lastDl = now; refresh(ctx) }
    }

    /** Tegn alle widgetene på nytt i sin egen størrelse (billig: ingen nettverk). */
    fun refresh(ctx: Context) {
        val am = AppWidgetManager.getInstance(ctx)
        for ((cls, kind) in KINDS) for (id in am.getAppWidgetIds(ComponentName(ctx, cls)))
            runCatching { am.updateAppWidget(id, views(ctx, kind, am.getAppWidgetOptions(id))) }
        ClockTick.schedule(ctx)
    }

    private val KINDS = listOf(ComboWidget::class.java to "big", ClockWidget::class.java to "clock",
        PcWidget::class.java to "pc", DownloadsWidget::class.java to "dl")

    fun hasClock(ctx: Context): Boolean {
        val am = AppWidgetManager.getInstance(ctx)
        return listOf(ComboWidget::class.java, ClockWidget::class.java).any { am.getAppWidgetIds(ComponentName(ctx, it)).isNotEmpty() }
    }

    // --- tegning ----------------------------------------------------------------------------------------
    private fun open(ctx: Context, tab: String, code: Int): PendingIntent =
        PendingIntent.getActivity(ctx, code, Intent(ctx, MainActivity::class.java).putExtra("tab", tab)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)

    /** Pikselstørrelse fra widgetens størrelse (stående: minste bredde × største høyde), med et tak på minnet. */
    private fun size(ctx: Context, o: Bundle?, kind: String): Pair<Int, Int> {
        val dens = ctx.resources.displayMetrics.density
        var wdp = o?.getInt(AppWidgetManager.OPTION_APPWIDGET_MIN_WIDTH)?.takeIf { it > 0 } ?: 380
        var hdp = o?.getInt(AppWidgetManager.OPTION_APPWIDGET_MAX_HEIGHT)?.takeIf { it > 0 }
            ?: when (kind) { "big" -> 400; "pc" -> 165; else -> 150 }
        wdp = wdp.coerceIn(120, 600); hdp = hdp.coerceIn(60, 700)
        var scale = dens
        while (wdp * hdp * scale * scale > 1_600_000) scale *= 0.9f
        return (wdp * scale).toInt() to (hdp * scale).toInt()
    }

    private fun views(ctx: Context, kind: String, o: Bundle?): RemoteViews {
        val (w, h) = size(ctx, o, kind)
        val a = WidgetArt(ctx, w, h)
        val v = RemoteViews(ctx.packageName, R.layout.w_canvas)
        val tab = when (kind) { "pc" -> "pc"; "dl" -> "media"; else -> "hjem" }
        val base = when (kind) { "big" -> 10; "clock" -> 20; "pc" -> 30; else -> 40 }
        for ((i, id) in listOf(R.id.w_top, R.id.w_a, R.id.w_b, R.id.w_c, R.id.w_d).withIndex())
            v.setOnClickPendingIntent(id, open(ctx, tab, base + i))
        when (kind) {
            "big" -> drawBig(ctx, a)
            "clock" -> drawClock(a)
            "pc" -> drawPc(ctx, a)
            else -> drawDownloads(ctx, a)
        }
        v.setImageViewBitmap(R.id.w_img, a.bmp)
        return v
    }

    private fun rate(b: Long): String = when {
        b < 0 -> "?"
        b >= 1_000_000 -> "%.1f MB/s".format(NB, b / 1e6)
        b >= 1_000 -> "%.0f kB/s".format(NB, b / 1e3)
        else -> "$b B/s"
    }

    private fun online(ctx: Context): Pair<String, Int> {
        val p = prefs(ctx)
        return if (p.getLong("pc_at", 0L) == 0L) "venter" to WidgetArt.MUTED
        else if (p.getBoolean("pc_online", false)) "● online" to WidgetArt.OK else "○ ingen kontakt" to WidgetArt.MUTED
    }

    /** Klokka (ASCII-sifre) midtstilt med dato under. cw = cellebredde. */
    private fun clockBlock(a: WidgetArt, y0: Float, cw: Float) {
        val t = LocalTime.now()
        val s = "%02d:%02d".format(t.hour, t.minute)
        a.fig(s, (a.w - WidgetArt.figWidth(s, cw)) / 2, y0, cw)
        val d = LocalDate.now()
        val date = "${d.dayOfWeek.getDisplayName(TextStyle.FULL, NB)} ${d.dayOfMonth}. ${d.month.getDisplayName(TextStyle.FULL, NB)}".uppercase(NB)
        val ds = a.u * 5.4f
        a.text(date, (a.w - a.textPaint(ds, WidgetArt.MUTED).measureText(date)) / 2, y0 + cw * 12 + a.u * 9, ds, WidgetArt.MUTED)
    }

    /** GPU-temperaturen i ASCII-sifre til venstre; GPU/CPU/disk-målere og nettverk til høyre – innenfor [y0, y1]. */
    private fun pcBlock(ctx: Context, a: WidgetArt, y0: Float, y1: Float) {
        val p = prefs(ctx)
        val gpu = p.getFloat("pc_gpu", -1f).toDouble().takeIf { it >= 0 }
        val cpu = p.getFloat("pc_cpu", -1f).toDouble().takeIf { it >= 0 }
        val disk = p.getInt("pc_disk", -1).takeIf { it >= 0 }
        val u = a.u
        val cw = minOf((y1 - y0 - u * 10) / 12f, (a.right - a.left) * 0.40f / 19f)
        val dw = a.fig(gpu?.let { "%.0f°".format(it) } ?: "00°", a.left, y0, cw, if (gpu == null) WidgetArt.LINE else WidgetArt.heat(gpu))
        a.text("GPU-TEMP", a.left, y0 + cw * 12 + u * 5.5f, u * 4.2f, WidgetArt.MUTED)
        val mx = a.left + dw + u * 8
        val mw = a.right - mx
        val ms = u * 5f
        val step = minOf((y1 - y0 - ms) / 3.4f, ms * 2.1f)
        val my = y0 + ms
        a.meter("GPU", (gpu ?: 0.0) / 100, gpu?.let { "%.0f°".format(it) } ?: "?", mx, my, mw, ms, WidgetArt.heat(gpu))
        a.meter("CPU", (cpu ?: 0.0) / 100, cpu?.let { "%.0f°".format(it) } ?: "?", mx, my + step, mw, ms, WidgetArt.heat(cpu))
        a.meter("DSK", (disk ?: 0) / 100.0, disk?.let { "$it %" } ?: "?", mx, my + step * 2, mw, ms,
            if ((disk ?: 0) > 90) WidgetArt.BAD else WidgetArt.GOLD)
        a.text("NET", mx, my + step * 3, ms, WidgetArt.MUTED)
        var x = mx + a.textPaint(ms, WidgetArt.MUTED).measureText("GPU ")
        x += a.text("↓ ${rate(p.getLong("net_down", -1))}", x, my + step * 3, ms, WidgetArt.GOLD, glow = true) + u * 4
        a.text("↑ ${rate(p.getLong("net_up", -1))}", x, my + step * 3, ms, WidgetArt.MUTED)
    }

    /** Nedlastingene: tittel, måler og prosent; type · kilde · fart under. */
    private fun dlRows(ctx: Context, a: WidgetArt, y0: Float, size: Float, max: Int) {
        val arr = runCatching { JSONArray(prefs(ctx).getString("dl", "[]")) }.getOrNull() ?: JSONArray()
        val u = a.u
        if (arr.length() == 0) { a.text("ingen nedlastinger nå", a.left, y0, size, WidgetArt.DIM); return }
        var y = y0
        val kinds = mapOf("music" to "musikk", "film" to "film", "show" to "serie", "audiobook" to "lydbok", "ebook" to "e-bok")
        for (i in 0 until minOf(arr.length(), max)) {
            val d = arr.getJSONObject(i)
            val span = a.right - a.left
            a.text("↓ ${d.optString("t")}", a.left, y, size, WidgetArt.TEXT, maxW = span * 0.52f)
            val frac = d.optDouble("p", 0.0)
            a.meter("", frac, "${(frac * 100).toInt()} %", a.left + span * 0.55f, y, span * 0.45f, size, WidgetArt.GOLD)
            val sub = listOfNotNull(kinds[d.optString("k")], d.optString("m").takeIf { it.isNotEmpty() },
                d.optLong("r", 0).takeIf { it > 0 }?.let { rate(it) }).joinToString(" · ")
            a.text(sub, a.left + u * 4, y + size * 1.15f, size * 0.75f, WidgetArt.DIM)
            y += size * 2.6f
        }
    }

    private fun drawBig(ctx: Context, a: WidgetArt) {
        val u = a.u
        val (st, col) = online(ctx)
        a.corners(t("ϟ ZEUS"), st, col)
        val cw = minOf(a.h * 0.021f, (a.right - a.left) * 0.84f / 37f)
        clockBlock(a, u * 21, cw)
        val y = u * 21 + cw * 12 + u * 17
        a.line(y)
        val y1 = y + u * 8 + a.h * 0.24f
        pcBlock(ctx, a, y + u * 8, y1)
        val y2 = y1 + u * 6
        a.line(y2)
        dlRows(ctx, a, y2 + u * 10, u * 5.4f, 2)
    }

    private fun drawClock(a: WidgetArt) {
        val u = a.u
        val d = LocalDate.now()
        a.corners(t("ϟ ZEUS"), t("uke %s", d.get(java.time.temporal.IsoFields.WEEK_OF_WEEK_BASED_YEAR)))
        val cw = minOf((a.h - u * 44) / 12f, (a.right - a.left) * 0.84f / 37f)
        clockBlock(a, u * 19, cw)
    }

    private fun drawPc(ctx: Context, a: WidgetArt) {
        val (st, col) = online(ctx)
        a.corners(t("◈ PC"), st, col)
        // Høyere enn innholdet: midtstill det loddrett i stedet for å la bunnen stå tom
        val u = a.u
        val cw = (a.right - a.left) * 0.40f / 19f
        val content = cw * 12 + u * 10
        val free = (a.h - u * 12 - u * 17) - content
        val y0 = u * 17 + maxOf(0f, free / 2)
        pcBlock(ctx, a, y0, if (free > 0) y0 + content + u * 10 else a.h - u * 12)
    }

    private fun drawDownloads(ctx: Context, a: WidgetArt) {
        val n = runCatching { JSONArray(prefs(ctx).getString("dl", "[]")).length() }.getOrDefault(0)
        a.corners(t("↓ NEDLASTINGER"), if (n == 1) t("1 aktiv") else "$n aktive")
        dlRows(ctx, a, a.u * 26, a.u * 6.4f, 3)
    }
}

/** Klokka: tegn på nytt ved hvert minuttskifte (RTC = vekker ikke telefonen; tegnes når skjermen er på). */
class ClockTick : BroadcastReceiver() {
    companion object {
        private const val ACTION = "no.olympus.zeus.widget.TICK"

        fun schedule(ctx: Context) {
            val am = ctx.getSystemService(AlarmManager::class.java) ?: return
            val pi = PendingIntent.getBroadcast(ctx, 77, Intent(ctx, ClockTick::class.java).setAction(ACTION),
                PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
            if (!Widgets.hasClock(ctx)) { am.cancel(pi); return }
            val now = System.currentTimeMillis()
            val next = now - now % 60_000 + 60_000
            if (am.canScheduleExactAlarms()) am.setExact(AlarmManager.RTC, next, pi) else am.setWindow(AlarmManager.RTC, next, 5_000, pi)
        }
    }

    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action == ACTION || intent.action == Intent.ACTION_TIME_CHANGED || intent.action == Intent.ACTION_TIMEZONE_CHANGED)
            Widgets.refresh(context.applicationContext)
    }
}

/** Felles for alle: tegn ved oppstart, ved endret størrelse og hver halvtime. */
open class ZeusWidget : AppWidgetProvider() {
    override fun onUpdate(context: Context, manager: AppWidgetManager, ids: IntArray) = Widgets.refresh(context)
    override fun onAppWidgetOptionsChanged(context: Context, manager: AppWidgetManager, id: Int, newOptions: Bundle) =
        Widgets.refresh(context)
    override fun onDisabled(context: Context) = ClockTick.schedule(context)  // ingen klokke igjen → stopp
}

class ComboWidget : ZeusWidget()
class ClockWidget : ZeusWidget()
class PcWidget : ZeusWidget()
class DownloadsWidget : ZeusWidget()
