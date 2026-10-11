package no.olympus.zeus

import android.content.Context
import android.content.Intent
import android.net.Uri
import android.provider.Settings
import androidx.core.content.FileProvider
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.Request
import org.json.JSONObject
import java.io.File

/** Ny versjon av appen fra PC-en: Olympus har den nyeste APK-en, appen spør ved oppstart og installerer med ett trykk
 *  (Android viser sin egen «Oppdater?»-dialog; samme signatur, så data og paring beholdes). */
object Updater {
    /** Ny versjon på PC-en, eller null. */
    suspend fun check(hub: Hub): JSONObject? = runCatching { hub.api.get("/app") }.getOrNull()
        ?.takeIf { it.optInt("code") > BuildConfig.VERSION_CODE }

    suspend fun install(context: Context, store: Store): String? = withContext(Dispatchers.IO) {
        val dir = File(context.cacheDir, "updates").apply { mkdirs() }
        val apk = File(dir, "zeus.apk")
        runCatching {
            authedClient(store).newCall(Request.Builder().url("http://${store.host}/api/v1/app/apk").build()).execute().use { r ->
                if (!r.isSuccessful) error("HTTP ${r.code}")
                apk.outputStream().use { out -> r.body!!.byteStream().copyTo(out) }
            }
        }.exceptionOrNull()?.let { return@withContext t("Fikk ikke hentet den nye versjonen (%s).", it.message) }
        withContext(Dispatchers.Main) {
            if (!context.packageManager.canRequestPackageInstalls()) {
                // Første gang: Android vil at du gir Zeus lov til å installere oppdateringer.
                context.startActivity(Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.parse("package:${context.packageName}"))
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
                return@withContext t("Gi Zeus lov til å installere oppdateringer, og trykk [ oppdater ] igjen.")
            }
            val uri = FileProvider.getUriForFile(context, "${context.packageName}.updates", apk)
            context.startActivity(Intent(Intent.ACTION_VIEW).setDataAndType(uri, "application/vnd.android.package-archive")
                .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK))
            null
        }
    }
}
