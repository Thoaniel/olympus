package no.olympus.zeus

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONObject
import java.io.IOException
import java.util.concurrent.TimeUnit

class ApiError(val code: Int, message: String) : IOException(message)

/** REST og websocket mot Olympus (`/api/v1`). All trafikk går over Tailscale til PC-en. */
class Api(private val store: Store) {
    private val http = OkHttpClient.Builder()
        .connectTimeout(5, TimeUnit.SECONDS)
        .readTimeout(30, TimeUnit.SECONDS)
        .pingInterval(25, TimeUnit.SECONDS)
        .build()
    private val json = "application/json".toMediaType()

    private fun base(host: String = store.host) = "http://$host/api/v1"

    private fun request(path: String, host: String = store.host): Request.Builder {
        val b = Request.Builder().url(base(host) + path)
        val t = store.token
        if (t.isNotEmpty()) b.header("Authorization", "Bearer $t")
        return b
    }

    private suspend fun call(req: Request): JSONObject = withContext(Dispatchers.IO) {
        http.newCall(req).execute().use { r -> parse(r) }
    }

    private fun parse(r: Response): JSONObject {
        val text = r.body?.string().orEmpty()
        if (!r.isSuccessful) {
            val msg = runCatching { JSONObject(text).optString("error") }.getOrNull().orEmpty()
            throw ApiError(r.code, msg.ifEmpty { "HTTP ${r.code}" })
        }
        return if (text.trim().startsWith("[")) JSONObject().put("items", org.json.JSONArray(text)) else JSONObject(text.ifEmpty { "{}" })
    }

    suspend fun get(path: String) = call(request(path).get().build())
    suspend fun post(path: String, body: JSONObject = JSONObject()) = call(request(path).post(body.toString().toRequestBody(json)).build())
    suspend fun patch(path: String, body: JSONObject) = call(request(path).patch(body.toString().toRequestBody(json)).build())
    suspend fun put(path: String, body: JSONObject) = call(request(path).put(body.toString().toRequestBody(json)).build())
    suspend fun delete(path: String) = call(request(path).delete().build())

    /** Bytt engangskoden fra QR-en mot en egen nøkkel for denne telefonen. */
    suspend fun pair(host: String, code: String, name: String): JSONObject =
        call(Request.Builder().url(base(host) + "/pair").post(JSONObject().put("code", code).put("name", name).toString().toRequestBody(json)).build())

    private val streaming = http.newBuilder().readTimeout(0, TimeUnit.SECONDS).build()

    /** Rå strøm fra PC-en (videresenderen for casting borte). Kalleren lukker svaret. */
    fun stream(path: String, range: String?): Response =
        streaming.newCall(request(path).get().apply { if (range != null) header("Range", range) }.build()).execute()

    fun connect(listener: WebSocketListener): WebSocket =
        http.newWebSocket(request("/ws").build(), listener)
}
