package no.olympus.zeus

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.NetworkType
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import java.util.concurrent.TimeUnit

/** Varsler fra Zeus når appen ikke er åpen: svar som ble ferdige, godkjenninger som venter (mens forbindelsen holdes
 *  til svaret er ferdig) og ferdige nedlastinger (bakgrunnssjekk hvert 15. min – minstetiden Android tillater). */
object Notify {
    private const val ZEUS = "zeus"
    private const val DL = "nedlastinger"
    private const val ID_ANSWER = 501
    private const val ID_APPROVAL = 502

    private fun channels(ctx: Context) {
        val nm = ctx.getSystemService(NotificationManager::class.java) ?: return
        nm.createNotificationChannel(NotificationChannel(ZEUS, t("Svar og godkjenninger fra Zeus"), NotificationManager.IMPORTANCE_HIGH))
        nm.createNotificationChannel(NotificationChannel(DL, t("Ferdige nedlastinger"), NotificationManager.IMPORTANCE_DEFAULT))
    }

    private fun post(ctx: Context, channel: String, id: Int, title: String, text: String, tab: String) {
        channels(ctx)
        val open = PendingIntent.getActivity(ctx, id, Intent(ctx, MainActivity::class.java).putExtra("tab", tab)
            .addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP), PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val n = Notification.Builder(ctx, channel).setSmallIcon(R.drawable.ic_zeus_fg).setContentTitle(title)
            .setContentText(text).setStyle(Notification.BigTextStyle().bigText(text)).setContentIntent(open).setAutoCancel(true).build()
        runCatching { ctx.getSystemService(NotificationManager::class.java)?.notify(id, n) }
    }

    fun answer(ctx: Context, text: String) = post(ctx, ZEUS, ID_ANSWER, t("ϟ Zeus har svart"), text.take(300), "zeus")
    fun approval(ctx: Context, reason: String) = post(ctx, ZEUS, ID_APPROVAL, t("ϟ Zeus venter på et ja"), reason.take(200), "zeus")
    fun download(ctx: Context, title: String, id: Int) = post(ctx, DL, 600 + (id and 0xff), t("↓ Ferdig lastet ned"), title, "media")

    fun clearZeus(ctx: Context) {
        ctx.getSystemService(NotificationManager::class.java)?.apply { cancel(ID_ANSWER); cancel(ID_APPROVAL) }
    }

    fun schedule(ctx: Context) {
        val req = PeriodicWorkRequestBuilder<CheckWorker>(15, TimeUnit.MINUTES)
            .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build()).build()
        WorkManager.getInstance(ctx).enqueueUniquePeriodicWork("zeus-sjekk", ExistingPeriodicWorkPolicy.KEEP, req)
    }
}

class CheckWorker(ctx: Context, params: WorkerParameters) : CoroutineWorker(ctx, params) {
    override suspend fun doWork(): Result {
        val store = Store(applicationContext)
        if (!store.paired) return Result.success()
        val prefs = applicationContext.getSharedPreferences("zeus", Context.MODE_PRIVATE)
        val since = prefs.getLong("notify_since_ms", 0L).takeIf { it > 0 }?.let { it / 1000.0 }
            ?: (System.currentTimeMillis() / 1000.0)  // første gang: ikke varsle om gamle nedlastinger
        val r = runCatching { Api(store).get("/notify?since=$since") }.getOrElse { return Result.success() }
        val done = r.optJSONArray("downloads_done")
        for (i in 0 until (done?.length() ?: 0)) {
            val d = done!!.getJSONObject(i)
            Notify.download(applicationContext, d.optString("title"), d.optString("id").hashCode())
        }
        if (r.optInt("approvals_waiting") > 0) Notify.approval(applicationContext, t("Åpne Zeus for å svare."))
        prefs.edit().putLong("notify_since_ms", (r.optDouble("now", since) * 1000).toLong()).apply()
        return Result.success()
    }
}


/** Holder appen våken mens Zeus svarer og appen er i bakgrunnen (Android fryser ellers apper i bakgrunnen, og svaret
 *  kom aldri fram). «shortService»: høyst 3 minutter, stoppes så snart svaret er ferdig (Hub, «done»). */
class AnswerService : android.app.Service() {
    override fun onBind(intent: Intent?): android.os.IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val nm = getSystemService(NotificationManager::class.java)
        nm?.createNotificationChannel(NotificationChannel("svarer", t("Zeus svarer"), NotificationManager.IMPORTANCE_MIN))
        val n = Notification.Builder(this, "svarer").setSmallIcon(R.drawable.ic_zeus_fg).setContentTitle(t("ϟ Zeus svarer …"))
            .setOngoing(true).build()
        startForeground(503, n, android.content.pm.ServiceInfo.FOREGROUND_SERVICE_TYPE_SHORT_SERVICE)
        return START_NOT_STICKY
    }

    override fun onTimeout(startId: Int) = stopSelf()  // 3 min uten svar: gi opp (varslet kommer neste gang appen åpnes)

    companion object {
        fun start(ctx: Context) {
            if (android.os.Build.VERSION.SDK_INT < 34) return
            runCatching { ctx.startForegroundService(Intent(ctx, AnswerService::class.java)) }
        }

        fun stop(ctx: Context) = runCatching { ctx.stopService(Intent(ctx, AnswerService::class.java)) }
    }
}
