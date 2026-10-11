package no.olympus.zeus

import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.composed
import androidx.compose.ui.draw.drawWithContent
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Shadow
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.Font
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.sp

object Z {
    val bg = Color(0xFF0B0B0F)
    val surface = Color(0xFF111118)
    val line = Color(0xFF34344A)
    val dim = Color(0xFF3A3A4C)
    val text = Color(0xFFE8E6DF)
    val muted = Color(0xFF8A8696)
    val gold = Color(0xFFD4A73A)
    val ok = Color(0xFF8FBF6F)
    val bad = Color(0xFFE0605E)

    val mono = FontFamily(Font(R.font.jbmono_regular), Font(R.font.jbmono_bold, FontWeight.Bold))
    val body = TextStyle(fontFamily = mono, fontSize = 13.sp, lineHeight = 19.sp, color = text)

    fun glow(on: Boolean) = if (on) Shadow(gold.copy(alpha = 0.55f), Offset.Zero, 14f) else null
}

/** Skjermeffekten over hele appen: skannelinjer og mørke hjørner (middels), pluss flimmer og en rullende stripe
 *  (sterk). «av» tegner ingenting – bare ASCII-stilen står igjen. */
fun Modifier.crt(level: String): Modifier = when (level) {
    "av" -> this
    "sterk" -> this.crtStrong()
    else -> this.drawWithContent { drawContent(); scan(0.11f, 4f); vignette(0.38f, 0.75f) }
}

/** Bare «sterk» animerer (flimmer og rullende stripe); «middels» er et stillbilde over innholdet og koster
 *  ingenting når skjermen står stille. */
private fun Modifier.crtStrong(): Modifier = composed {
    val t = rememberInfiniteTransition(label = "crt")
    val roll by t.animateFloat(0f, 1f, infiniteRepeatable(tween(6000, easing = LinearEasing), RepeatMode.Restart), label = "roll")
    val flick by t.animateFloat(1f, 0.93f, infiniteRepeatable(tween(90), RepeatMode.Reverse), label = "flick")
    drawWithContent {
        drawContent()
        scan(0.30f, 3f)
        vignette(0.7f, 0.62f)
        val bandY = roll * (size.height + 240f) - 120f
        drawRect(Brush.verticalGradient(listOf(Color.Transparent, Z.gold.copy(alpha = 0.06f), Color.Transparent), bandY, bandY + 120f))
        drawRect(Color.Black.copy(alpha = (1f - flick) * 0.5f))
    }
}

private fun androidx.compose.ui.graphics.drawscope.DrawScope.scan(alpha: Float, gapDp: Float) {
    val gap = gapDp * density
    var y = 0f
    while (y < size.height) {
        drawRect(Color.Black.copy(alpha = alpha), topLeft = Offset(0f, y), size = androidx.compose.ui.geometry.Size(size.width, density))
        y += gap
    }
}

private fun androidx.compose.ui.graphics.drawscope.DrawScope.vignette(alpha: Float, radius: Float) {
    drawRect(Brush.radialGradient(listOf(Color.Transparent, Color.Black.copy(alpha = alpha)),
        center = Offset(size.width / 2, size.height * 0.45f), radius = size.maxDimension * radius))
}

@Composable
fun goldStyle(bold: Boolean = true, glow: Boolean = true) =
    Z.body.copy(color = Z.gold, fontWeight = if (bold) FontWeight.Bold else FontWeight.Normal, shadow = Z.glow(glow))
