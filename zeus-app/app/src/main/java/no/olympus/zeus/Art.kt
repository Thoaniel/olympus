package no.olympus.zeus

import androidx.compose.animation.core.Animatable
import androidx.compose.animation.core.FastOutSlowInEasing
import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.text.BasicText
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.composed
import androidx.compose.ui.draw.drawBehind
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.text.PlatformTextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.LineHeightStyle
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.em
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.delay
import kotlin.math.abs
import kotlin.math.sin

/** ASCII-kunst: linjehøyde = skriftstørrelse og ingen ekstra luft over/under, så blokker, strek og braille
 *  henger sammen (vanlig tekst har +6 sp linjeavstand – da ble logoen til løse striper). */
@Composable
fun Art(text: String, modifier: Modifier = Modifier, color: Color = Z.gold, size: Float = 10f, glow: Boolean = true,
        bold: Boolean = false) {
    BasicText(
        text.trimStart('\n').trimEnd(), modifier,
        style = Z.body.copy(
            color = color, fontSize = size.sp, lineHeight = 1.0.em, letterSpacing = 0.sp,
            fontWeight = if (bold) FontWeight.Bold else FontWeight.Normal, shadow = Z.glow(glow),
            platformStyle = PlatformTextStyle(includeFontPadding = false),
            lineHeightStyle = LineHeightStyle(LineHeightStyle.Alignment.Center, LineHeightStyle.Trim.Both),
        ),
        softWrap = false,
    )
}

object Ascii {
    const val LOGO = """
███████╗███████╗██╗   ██╗███████╗
╚══███╔╝██╔════╝██║   ██║██╔════╝
  ███╔╝ █████╗  ██║   ██║███████╗
 ███╔╝  ██╔══╝  ██║   ██║╚════██║
███████╗███████╗╚██████╔╝███████║
╚══════╝╚══════╝ ╚═════╝ ╚══════╝"""

    /** Generert som braille (2×4 prikker per tegn): konsentriske riller med en løkke, som en ekte finger. */
    const val FINGERPRINT = """
⠀⠀⠀⠀⠀⠀⠀⢀⣀⣠⠤⠤⣄⣀⡉⠐⠢⢄⡀⠀⠀⠀⠀⠀
⠀⠀⠀⠀⠀⡠⠚⠉⣀⡤⠤⠤⠤⣄⣈⠙⠲⢄⡈⠓⢄⠀⠀⠀
⠀⠀⠀⢠⠎⢀⡴⠋⢁⡠⠤⠤⠤⢤⣈⠙⠢⣀⠙⢦⡀⠣⡀⠀
⠀⠀⢠⠃⢠⠋⢠⠞⠁⣠⠤⠶⠦⢤⡈⠑⢄⠈⢦⠀⢳⡀⠱⠀
⠀⠀⡞⠀⡏⢀⡏⢀⡞⠁⡠⠖⠲⢄⠈⢆⠈⣇⠈⣇⠀⣧⠀⠃
⢠⠀⡇⠀⡇⠸⡇⠸⡇⠐⡇⠀⠀⢸⠂⢸⠀⣼⠀⡿⠀⣿⠀⣠
⠀⠀⢳⠀⣇⠀⡇⠀⣇⠀⢷⠀⠀⣸⠀⡞⠀⡇⠀⡇⠀⡇⠀⡇
⠀⠆⢸⠀⢹⠀⢻⠀⢹⠀⠘⣧⣴⠇⢠⠇⢰⠃⢸⠃⢸⠃⢠⠇
⠀⠀⠈⡇⠸⡄⠸⡄⠘⣇⠀⠈⠉⠀⡼⠀⡼⠀⡼⠀⣼⠀⠘⠀
⠀⠀⠀⢧⠀⣇⠀⢧⠀⠹⣆⠀⠀⣰⠃⢀⡇⢀⡇⢀⡇⠀⠀⠀
⠀⠀⠀⠈⠀⢸⡀⠸⡆⠀⠹⣷⡾⠋⠀⡼⠀⣸⠀⢸⠁⠀⠀⠀
⠀⠀⠀⠀⠀⠈⠃⠀⢻⡄⠀⠀⠀⠀⣰⠃⢀⠇⠀⠀⠀⠀⠀⠀"""

    const val VINYL = """
    ▄▄█████▄▄
  ▄██▀▀   ▀▀██▄
 ██▀  ▄▄▄▄▄  ▀██
██   █▀   ▀█   ██
██   █  ◉  █   ██
██   █▄   ▄█   ██
 ██▄  ▀▀▀▀▀  ▄██
  ▀██▄▄   ▄▄██▀
    ▀▀█████▀▀"""

    /** Spilleren uten cover: måne over åser (fra utkastet). */
    const val LANDSCAPE = """
░░░░░░░░░░░░░░░░░░░░░░░░▒▓██▓▒░░░░░░
░░░░░░░░░░░░░░░░░░░░░░░▓██████▓░░░░░
░░░░░░░░░░░░░░░░░░░░░░░▒▓███▓▒░░░░░░
░░▒▒░░░░░░░░░░▒▒▒░░░░░░░░░░░░░░▒▒▒░░
▓▓██▓▓▒▒▒▒▓▓██████▓▓▓▒▒▒▒▒▓▓▓████▓▓▓
████████████████████████████████████"""

    private val POSTERS = listOf(
        """
▓▓▓▓▓▓▓▓▓▓▓▓
▓▒░░░░░░░░▒▓
▓░ ▄████▄ ░▓
▓░ ██▀▀██ ░▓
▓░ ▀████▀ ░▓
▓▒░░░░░░░░▒▓
▓▓▓▓▓▓▓▓▓▓▓▓""",
        """
▓▓▓▓▓▓▓▓▓▓▓▓
▓░░░░░▄░░░░▓
▓░░░░███░░░▓
▓░░░█████░░▓
▓░▄███████░▓
▓▄█████████▓
▓▓▓▓▓▓▓▓▓▓▓▓""",
        """
▓▓▓▓▓▓▓▓▓▓▓▓
▓░▄▀▀▀▀▀▀▄░▓
▓▐  ▄██▄  ▌▓
▓▐ ██◉███ ▌▓
▓▐  ▀██▀  ▌▓
▓░▀▄▄▄▄▄▄▀░▓
▓▓▓▓▓▓▓▓▓▓▓▓""",
    )
    private val TINTS = listOf(Color(0xFFD4A73A), Color(0xFF6A8FBF), Color(0xFFBF6A6A), Color(0xFF8FBF6F), Color(0xFFA77AC2))

    /** Plakat for en film uten bilde: samme id gir alltid samme motiv og farge. */
    fun poster(id: String): Pair<String, Color> {
        val h = abs(id.hashCode())
        return POSTERS[h % POSTERS.size] to TINTS[(h / 7) % TINTS.size]
    }

    private const val SUN = """
  \   |   /
   .-'''-.
--(       )--
   `-...-'
  /   |   \"""
    private const val PARTLY = """
  \  /
_ /''.-.
  \_(   ).
  /(___(__)"""
    private const val CLOUD = """
     .--.
  .-(    ).
 (___.__)__)"""
    private const val RAIN = """
     .--.
  .-(    ).
 (___.__)__)
  ʻ ʻ ʻ ʻ ʻ
 ʻ ʻ ʻ ʻ ʻ"""
    private const val SNOW = """
     .--.
  .-(    ).
 (___.__)__)
  *  *  *  *
 *  *  *  *"""
    private const val THUNDER = """
     .--.
  .-(    ).
 (___.__)__)
   ϟ ʻ ʻ ϟ
  ʻ ϟ ʻ ʻ"""
    private const val FOG = """
 _ - _ - _ -
  _ - _ - _
 _ - _ - _ -"""

    fun weather(symbol: String): String = when {
        symbol.startsWith("clearsky") -> SUN
        symbol.startsWith("fair") || symbol.startsWith("partlycloudy") -> PARTLY
        "thunder" in symbol -> THUNDER
        "snow" in symbol || "sleet" in symbol -> SNOW
        "rain" in symbol -> RAIN
        "fog" in symbol -> FOG
        else -> CLOUD
    }

    const val ZEUS_OFF = """
┌─ ϟ ZEUS ER AV ──────────────────┐
│ Appen virker som vanlig uten AI:│
│  ▦ plan     legg til · endre    │
│  ♪ musikk   spill · spillelister│
│  ▶ film     spill · last ned    │
│  § bøker    les · hør           │
│  ◉ lyd      høyttaler · volum   │
│ Alt synkes fortsatt med PC-en.  │
└─────────────────────────────────┘"""
}

/** ▮▮▮▯▯: framdrift i spiller, bøker og «fortsett». */
fun blocks(fraction: Double, width: Int = 20): String {
    val n = (fraction.coerceIn(0.0, 1.0) * width).toInt()
    return "█".repeat(n) + "░".repeat(width - n)  // ▮▯ finnes ikke i skrifta (ble tomme bokser)
}

/** Equalizer som hopper når noe spilles og står stille (dempet) ellers – da tegnes den uten animasjon. */
@Composable
fun EqBars(playing: Boolean, modifier: Modifier = Modifier, bars: Int = 16, height: Dp = 40.dp, gap: Dp = 3.dp) {
    val phase = if (playing) {
        val t = rememberInfiniteTransition(label = "eq")
        t.animateFloat(0f, 1f, infiniteRepeatable(tween(1400, easing = LinearEasing), RepeatMode.Restart), label = "p").value
    } else 0f
    Row(modifier.height(height), horizontalArrangement = Arrangement.spacedBy(gap), verticalAlignment = Alignment.Bottom) {
        repeat(bars) { i ->
            val h = if (playing) 0.15f + 0.85f * abs(sin(((phase + i * 0.37f) * 2 * Math.PI + i).toFloat())) else 0.12f + 0.06f * (i % 3)
            Box(Modifier.weight(1f).fillMaxHeight(h).background(
                Brush.verticalGradient(listOf(Color(0xFFF0C75A), Z.gold, Color(0xFF7A5E1F))), alpha = if (playing) 1f else 0.4f))
        }
    }
}

/** Liten utgave i spillelinja nederst. */
@Composable
fun MiniEq(playing: Boolean) = EqBars(playing, Modifier.width(14.dp), bars = 3, height = 12.dp, gap = 2.dp)

/** Gylne ringer som brer seg ut fra fingeravtrykket (låseskjermen). */
fun Modifier.waves(): Modifier = composed {
    val t = rememberInfiniteTransition(label = "waves")
    val a by t.animateFloat(0f, 1f, infiniteRepeatable(tween(2200, easing = FastOutSlowInEasing), RepeatMode.Restart), label = "a")
    drawBehind {
        listOf(a, (a + 0.5f) % 1f).forEach { p ->
            val r = size.minDimension * (0.32f + 0.3f * p)  // innenfor boksen: ringene skal ikke over teksten rundt
            drawCircle(Z.gold.copy(alpha = 0.8f * (1f - p)), r, center, style = Stroke(1.dp.toPx()))
        }
    }
}

/** Puls på kunsten (fingeravtrykket «puster»). */
fun Modifier.breathe(): Modifier = composed {
    val t = rememberInfiniteTransition(label = "breathe")
    val a by t.animateFloat(0.65f, 1f, infiniteRepeatable(tween(1100), RepeatMode.Reverse), label = "a")
    graphicsLayer { alpha = a }
}

/** Rødt skinn rundt et kort som venter på deg (godkjenning). */
fun Modifier.alarm(color: Color = Z.bad): Modifier = composed {
    val t = rememberInfiniteTransition(label = "alarm")
    val a by t.animateFloat(0f, 1f, infiniteRepeatable(tween(800), RepeatMode.Reverse), label = "a")
    drawBehind {
        repeat(3) { i ->
            val grow = (i + 1) * 3.dp.toPx()
            drawRect(color.copy(alpha = 0.18f * a / (i + 1)), topLeft = Offset(-grow, -grow),
                size = Size(size.width + 2 * grow, size.height + 2 * grow), style = Stroke(grow))
        }
    }
}

/** Innhold som glir inn etter `delayMs` (morgenbriefen bygger seg opp ett kort om gangen). */
fun Modifier.rise(delayMs: Int): Modifier = composed {
    val p = remember { Animatable(0f) }
    LaunchedEffect(Unit) { delay(delayMs.toLong()); p.animateTo(1f, tween(450, easing = FastOutSlowInEasing)) }
    graphicsLayer { alpha = p.value; translationY = (1f - p.value) * 8.dp.toPx() }
}

/** Blinkende ▌. */
@Composable
fun Cursor(size: Int = 13) {
    val t = rememberInfiniteTransition(label = "cursor")
    val blink by t.animateFloat(0f, 1f, infiniteRepeatable(tween(1000, easing = LinearEasing), RepeatMode.Restart), label = "b")
    T("▌", Modifier.graphicsLayer { alpha = if (blink < 0.5f) 1f else 0f }, color = Z.gold, size = size, glow = true)
}

/** «GOD MORGEN.» skrives ut tegn for tegn, med blinkende markør etterpå. */
@Composable
fun Typed(text: String, size: Int = 22) {
    var n by remember(text) { mutableIntStateOf(0) }
    LaunchedEffect(text) { delay(100); while (n < text.length) { delay(70); n++ } }
    Row {
        T(text.take(n), color = Z.gold, size = size, bold = true, glow = true)
        Cursor(size)
    }
}

/** Logoen forskyves et øyeblikk hvert 9. sekund – ellers stille. */
fun Modifier.glitch(): Modifier = composed {
    val t = rememberInfiniteTransition(label = "glitch")
    val p by t.animateFloat(0f, 1f, infiniteRepeatable(tween(9000, easing = LinearEasing), RepeatMode.Restart), label = "g")
    graphicsLayer {
        translationX = when { p in 0.91f..0.93f -> -4.dp.toPx(); p in 0.93f..0.95f -> 5.dp.toPx(); p in 0.95f..0.97f -> -2.dp.toPx(); else -> 0f }
    }
}
