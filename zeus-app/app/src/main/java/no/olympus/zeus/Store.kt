package no.olympus.zeus

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import org.json.JSONArray
import org.json.JSONObject
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/** Det appen husker lokalt: serveren, enhetsnøkkelen (kryptert med en nøkkel i Android Keystore), planen
 *  (så den virker uten nett), endringer som venter på å bli sendt, og små valg som effekter og siste brief. */
class Store(context: Context) {
    private val prefs = context.getSharedPreferences("zeus", Context.MODE_PRIVATE)

    var host: String
        get() = prefs.getString("host", "") ?: ""
        set(v) = prefs.edit().putString("host", v).apply()

    var serverName: String
        get() = prefs.getString("server", "") ?: ""
        set(v) = prefs.edit().putString("server", v).apply()

    var token: String
        get() = prefs.getString("token", null)?.let { decrypt(it) } ?: ""
        set(v) = prefs.edit().putString("token", if (v.isEmpty()) null else encrypt(v)).apply()

    val paired: Boolean get() = host.isNotEmpty() && token.isNotEmpty()

    /** Første gang et tips vises: true. Deretter false (tipset vises bare én gang). */
    fun seenHint(name: String): Boolean {
        val seen = prefs.getBoolean("hint_$name", false)
        if (!seen) prefs.edit().putBoolean("hint_$name", true).apply()
        return seen
    }

    var readerSize: Int  // skriftstørrelse i e-bokleseren (sp)
        get() = prefs.getInt("reader_size", 17)
        set(v) = prefs.edit().putInt("reader_size", v).apply()

    var lang: String  // auto | nb | en
        get() = prefs.getString("lang", "auto") ?: "auto"
        set(v) = prefs.edit().putString("lang", v).apply()

    var effects: String
        get() = prefs.getString("effects", "middels") ?: "middels"
        set(v) = prefs.edit().putString("effects", v).apply()

    var lastBrief: String
        get() = prefs.getString("brief", "") ?: ""
        set(v) = prefs.edit().putString("brief", v).apply()

    var seq: Int
        get() = prefs.getInt("seq", 0)
        set(v) = prefs.edit().putInt("seq", v).apply()

    var events: List<Event>
        get() = runCatching { JSONArray(prefs.getString("events", "[]")).let { a -> (0 until a.length()).map { Event.from(a.getJSONObject(it)) } } }.getOrDefault(emptyList())
        set(v) = prefs.edit().putString("events", JSONArray(v.map { it.toJson() }).toString()).apply()

    /** Endringer gjort uten kontakt med PC-en; sendes i rekkefølge når forbindelsen er tilbake. */
    var pending: List<JSONObject>
        get() = runCatching { JSONArray(prefs.getString("pending", "[]")).let { a -> (0 until a.length()).map { a.getJSONObject(it) } } }.getOrDefault(emptyList())
        set(v) = prefs.edit().putString("pending", JSONArray(v).toString()).apply()

    fun unpair() {
        prefs.edit().clear().apply()
    }

    // --- Keystore: nøkkelen til Olympus lagres aldri i klartekst på telefonen ---------------------------
    private fun key(): SecretKey {
        val ks = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (ks.getKey(ALIAS, null) as? SecretKey)?.let { return it }
        val gen = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore")
        gen.init(
            KeyGenParameterSpec.Builder(ALIAS, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .build()
        )
        return gen.generateKey()
    }

    private fun encrypt(plain: String): String {
        val c = Cipher.getInstance("AES/GCM/NoPadding").apply { init(Cipher.ENCRYPT_MODE, key()) }
        val out = c.iv + c.doFinal(plain.toByteArray())
        return Base64.encodeToString(out, Base64.NO_WRAP)
    }

    private fun decrypt(stored: String): String? = runCatching {
        val all = Base64.decode(stored, Base64.NO_WRAP)
        val c = Cipher.getInstance("AES/GCM/NoPadding")
        c.init(Cipher.DECRYPT_MODE, key(), GCMParameterSpec(128, all.copyOfRange(0, 12)))
        String(c.doFinal(all.copyOfRange(12, all.size)))
    }.getOrNull()

    companion object {
        private const val ALIAS = "zeus-token"
    }
}

data class Event(
    val id: String,
    val title: String,
    val start: String,
    val end: String?,
    val allDay: Boolean,
    val note: String,
    val source: String,
    val deleted: Boolean = false,
    val series: String = "",  // gjentatt avtale: felles for alle forekomstene
) {
    val date: String get() = start.take(10)
    val time: String get() = if (allDay || start.length < 16) t("hele dagen") else start.substring(11, 16)

    fun toJson(): JSONObject = JSONObject().put("id", id).put("title", title).put("start", start).put("end", end ?: JSONObject.NULL)
        .put("all_day", allDay).put("note", note).put("source", source).put("deleted", deleted).put("series", series)

    companion object {
        fun from(o: JSONObject) = Event(
            id = o.getString("id"), title = o.optString("title"), start = o.optString("start"),
            end = o.optString("end").takeIf { it.isNotEmpty() && it != "null" }, allDay = o.optBoolean("all_day"),
            note = o.optString("note"), source = o.optString("source", "du"), deleted = o.optBoolean("deleted"),
            series = o.optString("series"),
        )
    }
}
