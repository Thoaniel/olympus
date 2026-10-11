package no.olympus.zeus

import android.content.Context
import android.graphics.Bitmap
import android.graphics.BlurMaskFilter
import android.graphics.Canvas
import android.graphics.Paint
import android.graphics.RectF
import android.graphics.Typeface
import androidx.core.content.res.ResourcesCompat

/** Tegner widgetene som bilder i terminalstil: mørk flate, tynn ramme med tittel i streken, monospace i gull med
 *  glød, store blokk-sifre og ASCII-målere, svake skannelinjer. Samme farger som appen (Theme.kt). */
class WidgetArt(ctx: Context, val w: Int, val h: Int) {
    companion object {
        const val BG = 0xFF0B0B0F.toInt()
        const val GOLD = 0xFFD4A73A.toInt()
        const val TEXT = 0xFFE8E4F0.toInt()
        const val MUTED = 0xFF8A8696.toInt()
        const val DIM = 0xFF5A5868.toInt()
        const val LINE = 0xFF34344A.toInt()
        const val OK = 0xFF8FBF6F.toInt()
        const val WARM = 0xFFE0903A.toInt()
        const val BAD = 0xFFE0605E.toInt()

        /** 3×5 blokk-sifre (som i terminalen). */
        private val DIGITS = mapOf(
            '0' to listOf("###", "# #", "# #", "# #", "###"), '1' to listOf(" # ", "## ", " # ", " # ", "###"),
            '2' to listOf("###", "  #", "###", "#  ", "###"), '3' to listOf("###", "  #", " ##", "  #", "###"),
            '4' to listOf("# #", "# #", "###", "  #", "  #"), '5' to listOf("###", "#  ", "###", "  #", "###"),
            '6' to listOf("###", "#  ", "###", "# #", "###"), '7' to listOf("###", "  #", "  #", " # ", " # "),
            '8' to listOf("###", "# #", "###", "# #", "###"), '9' to listOf("###", "# #", "###", "  #", "###"),
            ':' to listOf(" ", "#", " ", "#", " "), '°' to listOf("##", "##", "  ", "  ", "  "),
            '%' to listOf("# #", "  #", " # ", "#  ", "# #"), '?' to listOf("###", "  #", " ##", "   ", " # "),
            '-' to listOf("   ", "   ", "###", "   ", "   "), ' ' to listOf(" ", " ", " ", " ", " "))

        const val SHADE = 0xFF806228.toInt()  // skyggestrekene i ASCII-sifrene

        /** ANSI Shadow-sifrene (samme skrift som ZEUS-logoen): █ = blokk, ╗═║╝╔╚ = skygge. */
        val FIG: Map<Char, List<String>> = mapOf(
            '0' to listOf(" ██████╗ ", "██╔═████╗", "██║██╔██║", "████╔╝██║", "╚██████╔╝", " ╚═════╝ "),
            '1' to listOf(" ██╗", "███║", "╚██║", " ██║", " ██║", " ╚═╝"),
            '2' to listOf("██████╗ ", "╚════██╗", " █████╔╝", "██╔═══╝ ", "███████╗", "╚══════╝"),
            '3' to listOf("██████╗ ", "╚════██╗", " █████╔╝", " ╚═══██╗", "██████╔╝", "╚═════╝ "),
            '4' to listOf("██╗  ██╗", "██║  ██║", "███████║", "╚════██║", "     ██║", "     ╚═╝"),
            '5' to listOf("███████╗", "██╔════╝", "███████╗", "╚════██║", "███████║", "╚══════╝"),
            '6' to listOf(" ██████╗ ", "██╔════╝ ", "███████╗ ", "██╔═══██╗", "╚██████╔╝", " ╚═════╝ "),
            '7' to listOf("███████╗", "╚════██║", "    ██╔╝", "   ██╔╝ ", "   ██║  ", "   ╚═╝  "),
            '8' to listOf(" █████╗ ", "██╔══██╗", "╚█████╔╝", "██╔══██╗", "╚█████╔╝", " ╚════╝ "),
            '9' to listOf(" █████╗ ", "██╔══██╗", "╚██████║", " ╚═══██║", " █████╔╝", " ╚════╝ "),
            ':' to listOf("   ", "██╗", "╚═╝", "██╗", "╚═╝", "   "),
            '%' to listOf("██╗ ██╗", "╚═╝██╔╝", "  ██╔╝ ", " ██╔╝  ", "██╔╝██╗", "╚═╝ ╚═╝"),
            '°' to listOf("██╗", "╚═╝", "   ", "   ", "   ", "   "),
            ' ' to listOf("  ", "  ", "  ", "  ", "  ", "  ")
        )

        fun figWidth(t: String, cw: Float) = t.sumOf { FIG[it]?.get(0)?.length ?: 0 } * cw

        fun heat(t: Double?): Int = when {
            t == null -> MUTED; t < 65 -> GOLD; t < 82 -> WARM; else -> BAD
        }
    }

    val bmp: Bitmap = Bitmap.createBitmap(w, h, Bitmap.Config.ARGB_8888)
    val c = Canvas(bmp)
    private val mono: Typeface = ResourcesCompat.getFont(ctx, R.font.jbmono_regular) ?: Typeface.MONOSPACE
    private val bold: Typeface = ResourcesCompat.getFont(ctx, R.font.jbmono_bold) ?: Typeface.DEFAULT_BOLD
    // enhet: som i utkastet – 1 % av min(0,62·bredde, høyde); én rad høye widgets får større tekst
    val u = if (h > w * 0.3f) minOf(w * 0.62f, h.toFloat()) / 100f else h / 60f
    val pad = 5.5f * u

    /** Bakgrunn, skannelinjer og ramme med tittel/høyretekst i streken: ╭─ ◈ PC ──────── ● online ─╮ */
    fun frame(title: String, right: String = "", rightColor: Int = MUTED) {
        val r = RectF(1f, 1f, w - 1f, h - 1f)
        c.drawRoundRect(r, 4 * u, 4 * u, Paint(Paint.ANTI_ALIAS_FLAG).apply { color = BG; alpha = 240 })
        val scan = Paint().apply { color = 0xFF000000.toInt(); alpha = 60 }
        var y = 0f
        while (y < h) { c.drawRect(0f, y, w.toFloat(), y + u * 0.35f, scan); y += u * 1.1f }
        val border = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = LINE; style = Paint.Style.STROKE; strokeWidth = u * 0.45f }
        val ty = pad + 1.5f * u
        val tsize = 6.2f * u
        val tp = textPaint(tsize, GOLD, bold = true, glow = true)
        val tw = tp.measureText(" $title ")
        val rp = textPaint(tsize * 0.9f, rightColor)
        val rw = if (right.isNotEmpty()) rp.measureText(" $right ") else 0f
        // rammen med åpninger for tekstene
        val top = ty - tsize * 0.35f
        c.drawLine(pad, top, pad + 2 * u, top, border)
        c.drawLine(pad + 2 * u + tw, top, w - pad - 2 * u - rw, top, border)
        c.drawLine(w - pad - 2 * u, top, w - pad, top, border)
        c.drawLine(pad, top, pad, h - pad, border)
        c.drawLine(w - pad, top, w - pad, h - pad, border)
        c.drawLine(pad, h - pad, w - pad, h - pad, border)
        c.drawText(" $title ", pad + 2 * u, ty, tp)
        if (right.isNotEmpty()) c.drawText(" $right ", w - pad - 2 * u - rw, ty, rp)
    }

    fun textPaint(size: Float, color: Int, bold: Boolean = false, glow: Boolean = false) = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        typeface = if (bold) this@WidgetArt.bold else mono
        textSize = size
        this.color = color
        if (glow) setShadowLayer(size * 0.45f, 0f, 0f, (color and 0x00FFFFFF) or 0x99000000.toInt())
    }

    /** Tekst som kuttes med «…» hvis den er for lang. Returnerer bredden som ble brukt. */
    fun text(s: String, x: Float, y: Float, size: Float, color: Int, maxW: Float = w - x - pad - 2 * u,
             bold: Boolean = false, glow: Boolean = false): Float {
        val p = textPaint(size, color, bold, glow)
        var t = s
        if (p.measureText(t) > maxW) {
            while (t.isNotEmpty() && p.measureText("$t…") > maxW) t = t.dropLast(1)
            t = "$t…"
        }
        c.drawText(t, x, y, p)
        return p.measureText(t)
    }

    /** ASCII-sifre med skygge: █ tegnes som gull blokk med glød, skyggestrekene som tynne linjer i mørkere gull –
     *  som geometri, så alt henger sømløst sammen. cw = bredden på én tegncelle (høyden er 2·cw). Returnerer bredden. */
    fun fig(t: String, x: Float, y: Float, cw: Float, color: Int = GOLD, shade: Int = SHADE): Float {
        val ch = cw * 2
        val lt = (cw * 0.34f).coerceAtLeast(1f)
        val fill = Paint().apply { this.color = color }
        val glow = Paint(Paint.ANTI_ALIAS_FLAG).apply {
            this.color = color; alpha = 120; maskFilter = BlurMaskFilter(cw * 1.6f, BlurMaskFilter.Blur.NORMAL) }
        val sh = Paint().apply { this.color = shade }
        var cx = x
        for (letter in t) {
            val g = FIG[letter] ?: continue
            for ((r, line) in g.withIndex()) for ((k, z) in line.withIndex()) {
                val x0 = cx + k * cw; val y0 = y + r * ch; val mx = x0 + cw / 2; val my = y0 + ch / 2
                if (z == '█') {
                    val box = RectF(x0, y0, x0 + cw + 0.5f, y0 + ch + 0.5f); c.drawRect(box, glow); c.drawRect(box, fill)
                } else if (z != ' ') {
                    if (z in "═╗╝") c.drawRect(x0, my - lt / 2, mx + lt / 2, my + lt / 2, sh)
                    if (z in "═╔╚") c.drawRect(mx - lt / 2, my - lt / 2, x0 + cw, my + lt / 2, sh)
                    if (z in "║╗╔") c.drawRect(mx - lt / 2, my - lt / 2, mx + lt / 2, y0 + ch, sh)
                    if (z in "║╝╚") c.drawRect(mx - lt / 2, y0, mx + lt / 2, my + lt / 2, sh)
                }
            }
            cx += g[0].length * cw
        }
        return cx - x
    }

    /** Gull hjørner (ingen full ramme), tittel øverst til venstre og en liten tekst til høyre. */
    fun corners(title: String, right: String = "", rightColor: Int = MUTED) {
        val l = 8 * u
        val p = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = GOLD; strokeWidth = (u * 0.55f).coerceAtLeast(2f); strokeCap = Paint.Cap.SQUARE }
        val g = Paint(p).apply { alpha = 140; maskFilter = BlurMaskFilter(u * 1.2f, BlurMaskFilter.Blur.NORMAL) }
        c.drawRoundRect(RectF(1f, 1f, w - 1f, h - 1f), 4 * u, 4 * u, Paint(Paint.ANTI_ALIAS_FLAG).apply { color = BG; alpha = 236 })
        val scan = Paint().apply { color = 0xFF000000.toInt(); alpha = 55 }
        var sy = 0f
        while (sy < h) { c.drawRect(0f, sy, w.toFloat(), sy + u * 0.35f, scan); sy += u * 1.1f }
        for ((x, y, dx, dy) in listOf(listOf(pad, pad, 1f, 1f), listOf(w - pad, pad, -1f, 1f), listOf(pad, h - pad, 1f, -1f), listOf(w - pad, h - pad, -1f, -1f)))
            for (paint in listOf(g, p)) { c.drawLine(x, y, x + dx * l, y, paint); c.drawLine(x, y, x, y + dy * l, paint) }
        val ts = 5.4f * u
        text(title, pad + 11 * u, pad + ts * 0.95f, ts, GOLD, bold = true, glow = true)
        if (right.isNotEmpty()) {
            val rp = textPaint(ts * 0.9f, rightColor)
            c.drawText(right, w - pad - 11 * u - rp.measureText(right), pad + ts * 0.95f, rp)
        }
    }

    /** Store blokk-sifre (f.eks. «33°» eller «20:00»), med glød. Returnerer bredden. */
    fun digits(s: String, x: Float, y: Float, cell: Float, color: Int): Float {
        val fill = Paint(Paint.ANTI_ALIAS_FLAG).apply { this.color = color }
        val glow = Paint(Paint.ANTI_ALIAS_FLAG).apply {
            this.color = color; alpha = 110; maskFilter = BlurMaskFilter(cell * 1.4f, BlurMaskFilter.Blur.NORMAL)
        }
        var cx = x
        for (ch in s) {
            val g = DIGITS[ch] ?: continue
            for ((row, line) in g.withIndex()) for ((col, k) in line.withIndex()) if (k == '#') {
                val r = RectF(cx + col * cell, y + row * cell, cx + (col + 1) * cell - cell * 0.12f, y + (row + 1) * cell - cell * 0.12f)
                c.drawRect(r, glow); c.drawRect(r, fill)
            }
            cx += (g[0].length + 1) * cell
        }
        return cx - x - cell
    }

    /** Segmentmåler (som en VU-måler): «GPU ▮▮▮▮▮▯▯▯▯▯ 33°» – fylte segmenter i nivåfargen med glød, resten svake. */
    fun meter(label: String, frac: Double, value: String, x: Float, y: Float, width: Float, size: Float, color: Int) {
        val lp = textPaint(size, MUTED)
        c.drawText(label, x, y, lp)
        val lw = lp.measureText("GPU ")
        val vp = textPaint(size, color, glow = true)
        val vw = vp.measureText(" 100 %")
        val barX = x + lw
        val barW = width - lw - vw
        val seg = size * 0.30f
        val gap = size * 0.12f
        val n = ((barW - gap) / (seg + gap)).toInt().coerceAtLeast(3)
        val on = (frac.coerceIn(0.0, 1.0) * n).let { kotlin.math.round(it).toInt() }
        val top = y - size * 0.72f
        val bottom = y + size * 0.05f
        val fill = Paint(Paint.ANTI_ALIAS_FLAG).apply { this.color = color }
        val glow = Paint(Paint.ANTI_ALIAS_FLAG).apply {
            this.color = color; alpha = 120; maskFilter = BlurMaskFilter(size * 0.35f, BlurMaskFilter.Blur.NORMAL) }
        val off = Paint(Paint.ANTI_ALIAS_FLAG).apply { this.color = LINE }
        for (i in 0 until n) {
            val sx = barX + i * (seg + gap)
            val r = RectF(sx, top, sx + seg, bottom)
            if (i < on) { c.drawRect(r, glow); c.drawRect(r, fill) } else c.drawRect(r, off)
        }
        c.drawText(value, x + width - vp.measureText(value), y, vp)
    }

    /** EQ-søyler (▁▃▅▇) – stille og flate når ingenting spilles. */
    fun eq(seed: Int, playing: Boolean, x: Float, y: Float, size: Float, n: Int = 7) {
        val r = java.util.Random(seed.toLong())
        val s = (0 until n).joinToString("") { if (playing) "▁▂▃▄▅▆▇"[r.nextInt(7)].toString() else "▁" }
        c.drawText(s, x, y, textPaint(size, if (playing) GOLD else DIM, glow = playing))
    }

    /** Knapp i terminalstil: [ ⏭ ] sentrert i et område (samme område som det usynlige trykkfeltet). */
    fun button(label: String, left: Float, top: Float, right: Float, bottom: Float, size: Float, color: Int = GOLD) {
        val r = RectF(left + u, top + u, right - u, bottom - u)
        c.drawRoundRect(r, 2 * u, 2 * u, Paint(Paint.ANTI_ALIAS_FLAG).apply {
            this.color = LINE; style = Paint.Style.STROKE; strokeWidth = u * 0.45f })
        val p = textPaint(size, color, glow = true)
        c.drawText(label, r.centerX() - p.measureText(label) / 2, r.centerY() + size * 0.35f, p)
    }

    val left get() = pad + 4 * u
    val right get() = w - pad - 4 * u
    fun line(y: Float) = c.drawLine(left, y, right, y, Paint().apply { color = LINE; strokeWidth = (u * 0.4f).coerceAtLeast(1f) })
}
