package no.olympus.zeus

import android.app.PendingIntent
import android.content.Intent
import android.os.Build
import android.service.quicksettings.Tile
import android.service.quicksettings.TileService

/** «ϟ Zeus» i hurtiginnstillingene: ett trykk åpner Zeus rett i taleinput (snakk, rett, send). */
class ZeusTile : TileService() {
    override fun onStartListening() {
        qsTile?.apply { state = Tile.STATE_INACTIVE; label = "Zeus"; subtitle = "snakk"; updateTile() }
    }

    override fun onClick() {
        val open = Intent(this, MainActivity::class.java).putExtra("tab", "zeus").putExtra("voice", true)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP)
        if (Build.VERSION.SDK_INT >= 34) {
            startActivityAndCollapse(PendingIntent.getActivity(this, 7, open, PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT))
        } else {
            @Suppress("DEPRECATION") startActivityAndCollapse(open)
        }
    }
}
