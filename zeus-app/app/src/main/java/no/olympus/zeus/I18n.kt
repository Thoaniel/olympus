package no.olympus.zeus

import android.app.Application
import android.content.Context
import org.json.JSONObject
import java.util.Locale

/** Norsk og engelsk. Teksten i koden er norsk og er nøkkelen; engelsk ligger i assets/i18n/en.json. Språket følger
 *  telefonen (nb/nn/no → norsk, ellers engelsk) og kan overstyres i innstillingene. %s fylles med argumentene. */
object I18n {
    @Volatile var english = false
    private var en: Map<String, String> = emptyMap()

    fun init(ctx: Context) {
        val choice = ctx.getSharedPreferences("zeus", Context.MODE_PRIVATE).getString("lang", "auto") ?: "auto"
        english = when (choice) {
            "nb" -> false
            "en" -> true
            else -> Locale.getDefault().language !in setOf("nb", "nn", "no")
        }
        if (en.isEmpty()) en = runCatching {
            val o = JSONObject(ctx.assets.open("i18n/en.json").bufferedReader().readText())
            o.keys().asSequence().associateWith { o.getString(it) }.filterValues { it.isNotEmpty() }
        }.getOrDefault(emptyMap())
    }

    fun tr(fmt: String, args: Array<out Any?>): String {
        val s = if (english) en[fmt] ?: fmt else fmt
        if (args.isEmpty()) return s
        // Del formatet én gang og flett inn argumentene: et argument som selv inneholder «%s» (en tittel, en lenke)
        // skal aldri tolkes som en plassholder.
        val parts = s.split("%s")
        val sb = StringBuilder(parts[0])
        for (i in 1 until parts.size) sb.append(args.getOrNull(i - 1)?.toString() ?: "").append(parts[i])
        return sb.toString()
    }
}

fun t(fmt: String, vararg args: Any?): String = I18n.tr(fmt, args)

class ZeusApp : Application() {
    override fun onCreate() {
        super.onCreate()
        I18n.init(this)
    }
}
