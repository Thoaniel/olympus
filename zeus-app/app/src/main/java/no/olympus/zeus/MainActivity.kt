package no.olympus.zeus

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.hardware.biometrics.BiometricManager
import android.hardware.biometrics.BiometricPrompt
import android.os.Bundle
import android.os.CancellationSignal
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.SystemBarStyle
import androidx.activity.enableEdgeToEdge
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier

private const val GRACE_MS = 2 * 60 * 1000L

/** Zeus: lås opp → morgenbrief (første gang hver dag) → faner. Uparet: paringsskjermen. */
class MainActivity : ComponentActivity() {
    private lateinit var hub: Hub
    private lateinit var play: Playback
    private var screen by mutableStateOf("lock")
    private var startTab by mutableStateOf("hjem")
    private var pairLink by mutableStateOf<String?>(null)
    private var effects by mutableStateOf("middels")
    private var lockMsg by mutableStateOf<String?>(null)

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // Mørk app: alltid lyse ikoner i status- og navigasjonslinja (ellers forsvinner klokka på lyse telefoner).
        enableEdgeToEdge(SystemBarStyle.dark(android.graphics.Color.TRANSPARENT), SystemBarStyle.dark(android.graphics.Color.TRANSPARENT))
        hub = Hub(applicationContext)
        play = Playback(applicationContext, hub)
        effects = hub.store.effects
        hub.onUnpaired = { screen = "pair" }
        hub.onHandoff = { tracks, idx, ms -> if (tracks.isNotEmpty()) play.playTracks(tracks, idx, ms) }
        hub.onPaired = { pairLink = null; screen = if (hub.briefDue) "brief" else "main" }
        Reminders.channel(this)
        if (checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 1)
        }
        handle(intent)
        if (!hub.store.paired) screen = "pair"
        setContent {
            Box(Modifier.fillMaxSize().background(Z.bg).crt(effects)) {
                when (screen) {
                    "pair" -> PairScreen(hub, pairLink, onCancel = if (hub.store.paired) ({ pairLink = null; screen = "main" }) else null)
                    "lock" -> LockScreen(lockMsg, hub.store.serverName, ::authenticate)
                    "brief" -> BriefScreen(hub) { tab -> hub.briefSeen(); startTab = tab; screen = "main" }
                    else -> MainScreen(hub, play, startTab, effects, { effects = it; hub.store.effects = it }) {
                        hub.stop(); hub.store.unpair(); screen = "pair"
                    }
                }
            }
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handle(intent)
    }

    private fun handle(intent: Intent?) {
        // Fra en widget: åpne riktig fane (også når appen allerede er åpen)
        if (intent?.getBooleanExtra("voice", false) == true) { hub.voice = true; intent.removeExtra("voice") }
        intent?.getStringExtra("tab")?.let { t -> startTab = t; hub.navTo = t; intent.removeExtra("tab"); return }
        if (intent?.action == Intent.ACTION_SEND) {
            // Delt tekst: plukk ut første lenke. Den åpnes i «+ legg til» etter opplåsing (aldri lastet ned uten at du trykker).
            val text = intent.getStringExtra(Intent.EXTRA_TEXT).orEmpty()
            val url = Regex("""(https?://\S+|magnet:\?\S+)""").find(text)?.value
            if (url != null) {
                hub.sharedUrl = url.trimEnd('.', ',', ')', '»')
                startTab = "media"
            } else if (text.isNotBlank()) {
                // Annen tekst (et sitat, en adresse, en melding): rett i Zeus-feltet, så du kan spørre om den
                hub.draft = text.take(4000)
                startTab = "zeus"; hub.navTo = "zeus"
            }
            return
        }
        val data = intent?.data ?: return
        if (data.scheme == "zeus" && data.host == "pair") {
            // Lenken kobler aldri til av seg selv: den vises for bekreftelse, og er appen allerede paret,
            // først etter at du har låst opp.
            pairLink = data.toString()
            if (!hub.store.paired) screen = "pair"
        }
    }

    private var leftAt = 0L

    override fun onStart() {
        super.onStart()
        // Låses bare når du har vært borte en stund (2 min) – ikke ved et raskt blikk på en annen app.
        // En ventende paringslenke krever alltid opplåsing.
        if (hub.store.paired && leftAt > 0 && (System.currentTimeMillis() - leftAt > GRACE_MS || pairLink != null)) screen = "lock"
        hub.background = false; hub.stopWhenDone = false
        Notify.clearZeus(this)
        if (hub.store.paired) { hub.start(); play.connect(); Notify.schedule(this) }
    }

    override fun onResume() {
        super.onResume()
        if (screen == "lock") window.decorView.post { authenticate() } // dialogen trenger en synlig aktivitet
    }

    override fun onStop() {
        super.onStop()
        hub.background = true
        // Svarer Zeus akkurat nå (eller venter på et ja)? Hold forbindelsen til svaret er ferdig – da kommer det som varsel.
        if (hub.busy) { hub.stopWhenDone = true; AnswerService.start(this) } else hub.stop()
        leftAt = System.currentTimeMillis()
    }

    /** Fingeravtrykk med telefonens egen sikre opplåsing (f.eks. sensoren i skjermen). Har telefonen
     *  ingen biometri satt opp, brukes PIN/mønster. */
    private fun authenticate() {
        if (screen != "lock") return
        val ok = {
            lockMsg = null
            screen = if (pairLink != null) "pair" else if (hub.briefDue) "brief" else "main"
        }
        val auth = BiometricManager.Authenticators.BIOMETRIC_STRONG or BiometricManager.Authenticators.DEVICE_CREDENTIAL
        if (getSystemService(BiometricManager::class.java)?.canAuthenticate(auth) != BiometricManager.BIOMETRIC_SUCCESS) {
            // Ingen skjermlås på telefonen: Zeus åpner seg ikke uten (nøkkelen gir tilgang til PC-en).
            lockMsg = t("Sett opp fingeravtrykk eller PIN på telefonen først.")
            return
        }
        BiometricPrompt.Builder(this)
            .setTitle(t("Lås opp Zeus"))
            .setAllowedAuthenticators(auth)
            .build()
            .authenticate(CancellationSignal(), mainExecutor, object : BiometricPrompt.AuthenticationCallback() {
                override fun onAuthenticationSucceeded(result: BiometricPrompt.AuthenticationResult?) = ok()
            })
    }
}
