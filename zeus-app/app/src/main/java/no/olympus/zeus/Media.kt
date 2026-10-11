package no.olympus.zeus

import android.app.DownloadManager
import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.net.Uri
import android.util.LruCache
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.layout.ContentScale
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONArray
import org.json.JSONObject
import java.io.File

data class Track(val id: String, val title: String, val artist: String, val album: String, val albumId: String,
                 val duration: Double, val cover: Boolean) {
    companion object {
        fun from(o: JSONObject) = Track(o.getString("id"), o.optString("title"), o.optString("artist"), o.optString("album"),
            o.optString("album_id"), o.optDouble("duration", 0.0), o.optBoolean("cover"))
    }
}

data class Album(val id: String, val title: String, val artist: String, val year: Int, val tracks: Int, val coverTrack: String?) {
    companion object {
        fun from(o: JSONObject) = Album(o.getString("id"), o.optString("title"), o.optString("artist"), o.optInt("year"),
            o.optInt("tracks"), o.optString("cover_track").takeIf { it.isNotEmpty() && it != "null" })
    }
}

data class Playlist(val id: String, val name: String, val tracks: Int) {
    companion object {
        fun from(o: JSONObject) = Playlist(o.getString("id"), o.optString("name"), o.optInt("tracks"))
    }
}

data class Video(val id: String, val kind: String, val title: String, val year: Int, val show: String?, val season: Int,
                 val episode: Int, val duration: Double, val position: Double, val direct: Boolean) {
    val label: String get() = if (kind == "episode") "S%02dE%02d %s".format(season, episode, title) else title
    companion object {
        fun from(o: JSONObject) = Video(o.getString("id"), o.optString("kind"), o.optString("title"), o.optInt("year"),
            o.optString("show").takeIf { it.isNotEmpty() && it != "null" }, o.optInt("season"), o.optInt("episode"),
            o.optDouble("duration", 0.0), o.optDouble("position", 0.0), o.optBoolean("direct"))
    }
}

data class Book(val id: String, val kind: String, val title: String, val author: String, val duration: Double,
                val position: Double, val format: String) {
    companion object {
        fun from(o: JSONObject) = Book(o.getString("id"), o.optString("kind"), o.optString("title"), o.optString("author"),
            o.optDouble("duration", 0.0), o.optDouble("position", 0.0), o.optString("format"))
    }
}

fun <T> JSONArray?.list(f: (JSONObject) -> T): List<T> = if (this == null) emptyList() else (0 until length()).map { f(getJSONObject(it)) }

fun clock(seconds: Double): String {
    val s = seconds.toInt().coerceAtLeast(0)
    return if (s >= 3600) "%d:%02d:%02d".format(s / 3600, s % 3600 / 60, s % 60) else "%d:%02d".format(s / 60, s % 60)
}

fun bar(fraction: Double, width: Int = 16): String {
    val n = (fraction.coerceIn(0.0, 1.0) * width).toInt()
    return "█".repeat(n) + "░".repeat(width - n)
}

/** Bilder (cover, plakater) fra Olympus med enhetsnøkkelen, hurtigbufret i minnet. */
object Images {
    private val cache = LruCache<String, Bitmap>(40)
    var version by mutableStateOf(0)   // økes når et cover er byttet → alle bilder hentes på nytt

    fun bust() { cache.evictAll(); version++ }
    private val http = OkHttpClient()

    suspend fun load(store: Store, path: String): Bitmap? {
        cache.get(path)?.let { return it }
        return withContext(Dispatchers.IO) {
            runCatching {
                val req = Request.Builder().url("http://${store.host}/api/v1$path").header("Authorization", "Bearer ${store.token}").build()
                http.newCall(req).execute().use { r -> if (r.isSuccessful) r.body?.bytes()?.let { BitmapFactory.decodeByteArray(it, 0, it.size) } else null }
            }.getOrNull()?.also { cache.put(path, it) }
        }
    }
}

@Composable
fun RemoteImage(store: Store, path: String?, modifier: Modifier, placeholder: String = "♪",
                fallback: (@Composable () -> Unit)? = null) {
    val v = Images.version
    var bmp by remember(path, v) { mutableStateOf<Bitmap?>(null) }
    var tried by remember(path, v) { mutableStateOf(path == null) }
    LaunchedEffect(path, v) { if (path != null) { bmp = Images.load(store, path); tried = true } }
    Box(modifier.background(Z.surface), contentAlignment = Alignment.Center) {
        val b = bmp
        when {
            b != null -> Image(b.asImageBitmap(), null, Modifier.matchParentSize(), contentScale = ContentScale.Crop)
            tried && fallback != null -> fallback()  // ingen bilde: ASCII-kunst i stedet for en tom flate
            else -> T(placeholder, color = Z.dim, size = 18)
        }
    }
}

/** Nedlasting til telefonen (for bruk uten nett) med Androids nedlastingsbehandler og enhetsnøkkelen. */
object Offline {
    fun file(context: Context, id: String): File = File(context.getExternalFilesDir("offline"), id)

    fun has(context: Context, id: String) = file(context, id).let { it.exists() && it.length() > 0 }

    fun download(context: Context, store: Store, path: String, id: String, title: String) {
        val dm = context.getSystemService(DownloadManager::class.java) ?: return
        val req = DownloadManager.Request(Uri.parse("http://${store.host}/api/v1$path"))
            .addRequestHeader("Authorization", "Bearer ${store.token}")
            .setTitle(t("Zeus: %s", title))
            .setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED)
            .setDestinationInExternalFilesDir(context, "offline", id)
        dm.enqueue(req)
    }

    fun delete(context: Context, id: String) = file(context, id).delete()
}
