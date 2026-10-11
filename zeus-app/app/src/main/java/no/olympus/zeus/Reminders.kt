package no.olympus.zeus

import android.app.AlarmManager
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.app.Notification
import java.time.LocalDate
import java.time.LocalDateTime
import java.time.LocalTime
import java.time.ZoneId

/** Påminnelser 15 minutter før hver avtale de neste 7 dagene, og en kort morgenoversikt kl. 07:30 på dager med
 *  avtaler. Settes på telefonen, så de kommer også når PC-en er av eller telefonen er uten nett. Planlegges på nytt
 *  hver gang planen endres og etter omstart. Eksakte alarmer (USE_EXACT_ALARM): de upresise kunne komme mange
 *  minutter for sent når telefonen sov. */
object Reminders {
    private const val CHANNEL = "paaminnelser"
    private const val MORNING_CHANNEL = "morgen"
    private const val LEAD_MIN = 15L
    private const val SNOOZE_MIN = 10L
    private const val MAX = 40
    private const val MORNING_SLOT = 999
    private val MORNING_AT: LocalTime = LocalTime.of(7, 30)

    fun schedule(context: Context, events: List<Event>) {
        val am = context.getSystemService(AlarmManager::class.java) ?: return
        val now = System.currentTimeMillis()
        val upcoming = events.filter { !it.allDay && it.start.length >= 16 }.mapNotNull { ev ->
            runCatching {
                val at = LocalDateTime.parse(ev.start).minusMinutes(LEAD_MIN).atZone(ZoneId.systemDefault()).toInstant().toEpochMilli()
                ev to at
            }.getOrNull()
        }.filter { it.second in now..(now + 7L * 24 * 3600 * 1000) }.sortedBy { it.second }.take(MAX)
        for (i in 0 until MAX) am.cancel(intent(context, i, null))
        upcoming.forEachIndexed { i, (ev, at) -> alarm(am, at, intent(context, i, ev)) }
        // Morgenoversikten: neste 07:30 (i dag hvis den ikke har passert). Om det er noe å si, avgjøres da.
        val today = LocalDate.now().atTime(MORNING_AT)
        val next = if (LocalDateTime.now().isBefore(today)) today else today.plusDays(1)
        alarm(am, next.atZone(ZoneId.systemDefault()).toInstant().toEpochMilli(), morningIntent(context))
    }

    private fun alarm(am: AlarmManager, at: Long, pi: PendingIntent) {
        if (am.canScheduleExactAlarms()) am.setExactAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, at, pi)
        else am.setAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, at, pi)
    }

    private fun intent(context: Context, slot: Int, ev: Event?): PendingIntent {
        val i = Intent(context, ReminderReceiver::class.java)
        if (ev != null) i.putExtra("title", ev.title).putExtra("time", ev.time)
        return PendingIntent.getBroadcast(context, 1000 + slot, i, PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
    }

    private fun morningIntent(context: Context): PendingIntent =
        PendingIntent.getBroadcast(context, 1000 + MORNING_SLOT, Intent(context, ReminderReceiver::class.java).putExtra("morning", true),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)

    fun channel(context: Context) {
        val nm = context.getSystemService(NotificationManager::class.java) ?: return
        nm.createNotificationChannel(NotificationChannel(CHANNEL, t("Påminnelser"), NotificationManager.IMPORTANCE_HIGH))
        nm.createNotificationChannel(NotificationChannel(MORNING_CHANNEL, "Morgenoversikt", NotificationManager.IMPORTANCE_DEFAULT))
    }

    private fun openApp(context: Context, tab: String): PendingIntent =
        PendingIntent.getActivity(context, tab.hashCode(), Intent(context, MainActivity::class.java)
            .addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP).putExtra("tab", tab), PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)

    fun notify(context: Context, title: String, time: String, snoozed: Boolean = false) {
        channel(context)
        val id = title.hashCode()
        // «Utsett 10 min»: en ny påminnelse om 10 minutter, uten å åpne appen
        val snooze = PendingIntent.getBroadcast(context, id, Intent(context, ReminderReceiver::class.java)
            .putExtra("snooze", true).putExtra("title", title).putExtra("time", time),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val n = Notification.Builder(context, CHANNEL)
            .setSmallIcon(R.drawable.ic_zeus_fg)
            .setContentTitle(if (snoozed) t("⏰ %s", title) else t("⏰ %s om %s min", title, LEAD_MIN))
            .setContentText("kl. $time")
            .setContentIntent(openApp(context, "plan"))
            .addAction(Notification.Action.Builder(null, t("Utsett %s min", SNOOZE_MIN), snooze).build())
            .setAutoCancel(true)
            .build()
        context.getSystemService(NotificationManager::class.java)?.notify(id, n)
    }

    fun snooze(context: Context, title: String, time: String) {
        context.getSystemService(NotificationManager::class.java)?.cancel(title.hashCode())
        val am = context.getSystemService(AlarmManager::class.java) ?: return
        val pi = PendingIntent.getBroadcast(context, title.hashCode() xor 0x5a5a, Intent(context, ReminderReceiver::class.java)
            .putExtra("title", title).putExtra("time", time).putExtra("snoozed", true),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        alarm(am, System.currentTimeMillis() + SNOOZE_MIN * 60_000, pi)
    }

    /** Morgenoversikten fra PC-en (avtaler, vær, «fortsett», ferdige nedlastinger, PC-varsler). Svarer ikke PC-en,
     *  vises dagens avtaler fra telefonens egen kopi. Ingenting å si → ingen varsel. Kjøres utenfor hovedtråden. */
    fun morning(context: Context) {
        val store = Store(context)
        val events = store.events.filter { !it.deleted }
        schedule(context, events)  // neste morgen
        val today = LocalDate.now().toString()
        val todays = events.filter { it.date == today }.sortedBy { it.start }
        val fromPc = runCatching {
            kotlinx.coroutines.runBlocking { kotlinx.coroutines.withTimeout(8000) { Api(store).get("/brief/morning") } }
        }.getOrNull()
        val title: String
        val lines: List<String>
        if (fromPc != null) {
            title = fromPc.optString("title"); lines = fromPc.optJSONArray("lines")?.let { a -> (0 until a.length()).map { a.getString(it) } }.orEmpty()
        } else {
            title = t("ϟ I dag: %s %s", todays.size, if (todays.size == 1) "avtale" else "avtaler"); lines = todays.map { "${it.time}  ${it.title}" }
        }
        if (lines.isEmpty()) return
        channel(context)
        val n = Notification.Builder(context, MORNING_CHANNEL)
            .setSmallIcon(R.drawable.ic_zeus_fg)
            .setContentTitle(title)
            .setContentText(lines.joinToString(" · "))
            .setStyle(Notification.BigTextStyle().bigText(lines.joinToString("\n")))
            .setContentIntent(openApp(context, "plan"))
            .setAutoCancel(true)
            .build()
        context.getSystemService(NotificationManager::class.java)?.notify(MORNING_SLOT, n)
    }
}

class ReminderReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.getBooleanExtra("morning", false)) {  // henter fra PC-en: utenfor hovedtråden, med goAsync
            val pending = goAsync()
            kotlin.concurrent.thread { try { Reminders.morning(context) } finally { pending.finish() } }
            return
        }
        val title = intent.getStringExtra("title") ?: return
        val time = intent.getStringExtra("time").orEmpty()
        if (intent.getBooleanExtra("snooze", false)) Reminders.snooze(context, title, time)
        else Reminders.notify(context, title, time, snoozed = intent.getBooleanExtra("snoozed", false))
    }
}

class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action == Intent.ACTION_BOOT_COMPLETED) Reminders.schedule(context, Store(context).events.filter { !it.deleted })
    }
}
