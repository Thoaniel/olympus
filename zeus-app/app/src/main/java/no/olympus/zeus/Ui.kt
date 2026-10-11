package no.olympus.zeus

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.dp

/* Felles byggeklosser: alle faner bruker de samme, så logikken er lik overalt (og lik Lab's på PC-en).
 *  - SubHeader: «‹ tittel» øverst i en undervisning – trykk for å gå tilbake.
 *  - ListRow: én rad = ikon · tittel/undertittel · noe til høyre; trykk = hovedhandlingen.
 *  - Seg: velger mellom delene av en fane (musikk · film · bøker).
 *  - Group: overskrift med strek, innhold under – ingen rammer rundt alt. */

@Composable
fun SubHeader(title: String, onBack: () -> Unit, trailing: (@Composable () -> Unit)? = null) {
    Row(Modifier.fillMaxWidth().padding(horizontal = 6.dp), verticalAlignment = Alignment.CenterVertically) {
        Box(Modifier.clickable(onClick = onBack).padding(horizontal = 10.dp, vertical = 12.dp)) { T("‹", color = Z.gold, size = 18) }
        T(title, Modifier.weight(1f), color = Z.gold, bold = true, glow = true, maxLines = 1)
        trailing?.invoke()
    }
}

@OptIn(androidx.compose.foundation.ExperimentalFoundationApi::class)
@Composable
fun ListRow(title: String, modifier: Modifier = Modifier, icon: String = "", subtitle: String = "", trailing: String = "",
            on: Boolean = false, iconColor: Color = Z.gold, leading: (@Composable () -> Unit)? = null, onLongClick: (() -> Unit)? = null,
            onClick: (() -> Unit)? = null) {
    Row(modifier.fillMaxWidth().heightIn(min = 48.dp)
        .let { if (onClick != null || onLongClick != null) it.combinedClickable(onClick = { onClick?.invoke() }, onLongClick = onLongClick) else it }
        .padding(horizontal = 16.dp, vertical = 6.dp), verticalAlignment = Alignment.CenterVertically) {
        if (leading != null) { leading(); Spacer(Modifier.width(12.dp)) }
        else if (icon.isNotEmpty()) { T(icon, Modifier.width(22.dp), color = iconColor); Spacer(Modifier.width(6.dp)) }
        Column(Modifier.weight(1f)) {
            T(title, color = if (on) Z.gold else Z.text, bold = on, glow = on, maxLines = 1)
            if (subtitle.isNotEmpty()) T(subtitle, color = Z.muted, size = 11, maxLines = 2)
        }
        if (trailing.isNotEmpty()) T(trailing, color = Z.muted, size = 11)
    }
}

@Composable
fun Group(title: String, modifier: Modifier = Modifier, action: String = "", onAction: (() -> Unit)? = null,
          content: @Composable () -> Unit) {
    Column(modifier.fillMaxWidth().padding(top = 14.dp)) {
        Row(Modifier.padding(horizontal = 16.dp), verticalAlignment = Alignment.CenterVertically) {
            T(title, color = Z.gold, size = 12, bold = true)
            Spacer(Modifier.width(8.dp)); Rule(Modifier.weight(1f))
            if (action.isNotEmpty() && onAction != null) {
                Spacer(Modifier.width(8.dp))
                Box(Modifier.clickable(onClick = onAction).padding(6.dp)) { T(action, color = Z.gold, size = 12) }
            }
        }
        content()
    }
}

@Composable
fun Seg(options: List<String>, selected: String, modifier: Modifier = Modifier, small: Boolean = options.size > 3,
        inset: androidx.compose.ui.unit.Dp = 16.dp, onSelect: (String) -> Unit) {
    Row(modifier.fillMaxWidth().padding(horizontal = inset, vertical = 6.dp), horizontalArrangement = Arrangement.spacedBy(if (small) 4.dp else 8.dp)) {
        options.forEach { o ->
            val on = o == selected
            Box(Modifier.weight(1f).heightIn(min = 34.dp).border(1.dp, if (on) Z.gold else Z.line)
                .background(if (on) Z.gold.copy(alpha = 0.06f) else Z.bg).clickable { onSelect(o) }, contentAlignment = Alignment.Center) {
                T(t(o), color = if (on) Z.gold else Z.muted, bold = on, size = if (small) 10 else 12, maxLines = 1)
            }
        }
    }
}

/** Én prikk øverst til høyre: grønn = koblet til PC-en, gull = synker, grå = venter. Gull ring = noe nytt (oppdatering). */
@Composable
fun StatusDot(sync: Sync, badge: Boolean, onClick: () -> Unit) {
    val col = when (sync) { Sync.OK -> Z.ok; Sync.BUSY -> Z.gold; Sync.OFFLINE -> Z.muted }
    Box(Modifier.clickable(onClick = onClick).padding(10.dp), contentAlignment = Alignment.Center) {
        if (badge) Box(Modifier.size(18.dp).border(1.dp, Z.gold, androidx.compose.foundation.shape.CircleShape))
        Box(Modifier.size(10.dp).background(col, androidx.compose.foundation.shape.CircleShape))
    }
}
