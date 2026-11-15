package com.bkashbridge

import android.Manifest
import android.app.Activity
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Typeface
import android.net.Uri
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import android.provider.Telephony
import android.text.InputType
import android.view.View
import android.widget.Button
import android.widget.CheckBox
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import java.security.SecureRandom

class MainActivity : Activity() {

    private lateinit var etTopic: EditText
    private lateinit var etSecret: EditText
    private lateinit var etRelay: EditText
    private lateinit var cbAny: CheckBox
    private lateinit var tvStatus: TextView
    private lateinit var tvConfig: TextView
    private lateinit var tvLog: TextView
    private val h = Handler(Looper.getMainLooper())
    private val tick = object : Runnable {
        override fun run() {
            refresh()
            h.postDelayed(this, 3000)
        }
    }

    private fun dp(n: Int): Int = (n * resources.displayMetrics.density).toInt()

    private fun label(text: String, bold: Boolean = false, size: Float = 14f): TextView {
        val t = TextView(this)
        t.text = text
        t.textSize = size
        if (bold) t.setTypeface(null, Typeface.BOLD)
        t.setPadding(0, dp(8), 0, dp(2))
        return t
    }

    private fun field(hint: String, value: String): EditText {
        val e = EditText(this)
        e.hint = hint
        e.setText(value)
        e.setSingleLine(true)
        e.inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS
        return e
    }

    private fun button(text: String, onClick: () -> Unit): Button {
        val b = Button(this)
        b.text = text
        b.isAllCaps = false
        b.setOnClickListener { onClick() }
        return b
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val root = LinearLayout(this)
        root.orientation = LinearLayout.VERTICAL
        root.setPadding(dp(16), dp(24), dp(16), dp(24))

        root.addView(label("bKash Bridge", bold = true, size = 24f))
        root.addView(label("Forwards every bKash 'money received' SMS to your bot so orders confirm by themselves."))

        tvStatus = label("")
        root.addView(tvStatus)

        root.addView(label("1. Settings (same values go into the bot's config.txt)", bold = true))
        etTopic = field("Topic (secret name)", Store.topic(this))
        etSecret = field("Secret (password)", Store.secret(this))
        etRelay = field("Relay URL", Store.relay(this))
        root.addView(etTopic)
        root.addView(etSecret)
        root.addView(etRelay)
        cbAny = CheckBox(this)
        cbAny.text = "Test mode: forward 'received' SMS from ANY sender"
        cbAny.isChecked = Store.anySender(this)
        root.addView(cbAny)

        root.addView(button("Generate random Topic + Secret") {
            val r = SecureRandom()
            val chars = "abcdefghijklmnopqrstuvwxyz0123456789"
            fun rnd(n: Int): String = (1..n).map { chars[r.nextInt(chars.length)] }.joinToString("")
            etTopic.setText("bk-" + rnd(20))
            etSecret.setText(rnd(24))
            saveSettings()
        })
        root.addView(button("Save settings") { saveSettings() })

        root.addView(label("2. Copy these 2 lines into config.txt on your PC", bold = true))
        tvConfig = TextView(this)
        tvConfig.typeface = Typeface.MONOSPACE
        tvConfig.setTextIsSelectable(true)
        root.addView(tvConfig)
        root.addView(button("Copy the lines") {
            val cm = getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
            cm.setPrimaryClip(ClipData.newPlainText("config", configText()))
            toast("Copied")
        })

        root.addView(label("3. Permissions", bold = true))
        root.addView(button("Allow SMS permission") {
            requestPermissions(arrayOf(Manifest.permission.RECEIVE_SMS, Manifest.permission.READ_SMS), 1)
        })
        root.addView(button("Open App settings (allow restricted settings)") {
            startActivity(Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:$packageName")))
        })
        root.addView(button("Battery: set this app to 'Unrestricted'") {
            try {
                startActivity(Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS))
            } catch (e: Exception) {
                startActivity(Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:$packageName")))
            }
        })

        root.addView(label("4. Test", bold = true))
        root.addView(button("Send test message to the bot") {
            saveSettings()
            Forwarder.enqueue(this, "test", "TEST", "0", "app")
            Store.log(this, "Test message queued")
            refresh()
        })
        root.addView(button("Re-send bKash SMS of the last 3 days from inbox") { resendInbox() })

        root.addView(label("Activity", bold = true))
        tvLog = TextView(this)
        tvLog.typeface = Typeface.MONOSPACE
        tvLog.textSize = 12f
        root.addView(tvLog)

        val sv = ScrollView(this)
        sv.addView(root)
        setContentView(sv)
    }

    override fun onResume() {
        super.onResume()
        h.post(tick)
    }

    override fun onPause() {
        super.onPause()
        h.removeCallbacks(tick)
    }

    private fun toast(s: String) = Toast.makeText(this, s, Toast.LENGTH_SHORT).show()

    private fun saveSettings() {
        Store.save(this, etTopic.text.toString(), etSecret.text.toString(), etRelay.text.toString(), cbAny.isChecked)
        toast("Saved")
        refresh()
    }

    private fun configText(): String {
        val sb = StringBuilder()
        sb.append("SMS_TOPIC=").append(Store.topic(this)).append("\n")
        sb.append("SMS_SECRET=").append(Store.secret(this))
        if (Store.relay(this) != Store.DEFAULT_RELAY) sb.append("\nSMS_RELAY_URL=").append(Store.relay(this))
        return sb.toString()
    }

    private fun hasSmsPermission(): Boolean =
        checkSelfPermission(Manifest.permission.RECEIVE_SMS) == PackageManager.PERMISSION_GRANTED

    private fun refresh() {
        val perm = if (hasSmsPermission()) "OK" else "MISSING - tap 'Allow SMS permission'"
        val conf = if (Store.topic(this).isNotBlank() && Store.secret(this).isNotBlank()) "OK" else "NOT SET"
        tvStatus.text = "SMS permission: $perm\nSettings: $conf"
        tvConfig.text = configText()
        tvLog.text = Store.logText(this).ifEmpty { "(nothing yet)" }
    }

    private fun resendInbox() {
        if (checkSelfPermission(Manifest.permission.READ_SMS) != PackageManager.PERMISSION_GRANTED) {
            toast("Allow the SMS permission first")
            return
        }
        saveSettings()
        val ctx = this
        Thread {
            var n = 0
            try {
                val since = System.currentTimeMillis() - 3L * 24 * 3600 * 1000
                ctx.contentResolver.query(
                    Telephony.Sms.Inbox.CONTENT_URI,
                    arrayOf("address", "body", "date"),
                    "date > ?", arrayOf(since.toString()), "date DESC"
                )?.use { cur ->
                    while (cur.moveToNext() && n < 50) {
                        val a = cur.getString(0) ?: ""
                        val b = cur.getString(1) ?: ""
                        if (Forwarder.isBkash(a) && Forwarder.handle(ctx, a, b)) n++
                    }
                }
                Store.log(ctx, "Inbox scan finished: $n bKash payment SMS queued")
            } catch (e: Exception) {
                Store.log(ctx, "Inbox scan failed: ${e.javaClass.simpleName}")
            }
        }.start()
        toast("Scanning inbox...")
    }
}
