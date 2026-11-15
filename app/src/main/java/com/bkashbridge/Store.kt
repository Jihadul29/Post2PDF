package com.bkashbridge

import android.content.Context
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import javax.crypto.Mac
import javax.crypto.spec.SecretKeySpec

/** Settings + a small activity log kept in SharedPreferences. */
object Store {
    const val DEFAULT_RELAY = "https://ntfy.sh"

    private fun sp(c: Context) = c.getSharedPreferences("bridge", Context.MODE_PRIVATE)

    fun topic(c: Context): String = sp(c).getString("topic", "") ?: ""
    fun secret(c: Context): String = sp(c).getString("secret", "") ?: ""
    fun relay(c: Context): String {
        val r = (sp(c).getString("relay", DEFAULT_RELAY) ?: DEFAULT_RELAY).trim()
        return if (r.isEmpty()) DEFAULT_RELAY else r
    }
    fun anySender(c: Context): Boolean = sp(c).getBoolean("any", false)

    fun save(c: Context, topic: String, secret: String, relay: String, any: Boolean) {
        sp(c).edit()
            .putString("topic", topic.trim())
            .putString("secret", secret.trim())
            .putString("relay", relay.trim())
            .putBoolean("any", any)
            .commit()
    }

    @Synchronized
    fun log(c: Context, line: String) {
        val t = SimpleDateFormat("dd/MM HH:mm:ss", Locale.US).format(Date())
        val old = (sp(c).getString("log", "") ?: "").split("\n").filter { it.isNotBlank() }
        val all = (listOf("$t  $line") + old).take(80)
        sp(c).edit().putString("log", all.joinToString("\n")).commit()
    }

    fun logText(c: Context): String = sp(c).getString("log", "") ?: ""
}

/** Reads a bKash "money received" SMS. */
object Bkash {
    data class Payment(val trx: String, val amt: String, val from: String)

    private val TRX = Regex("""TrxID\s*[:\-]?\s*([A-Za-z0-9]{8,12})""", RegexOption.IGNORE_CASE)
    private val AMT = Regex("""(?:Tk|BDT|৳)\s*\.?\s*([0-9][0-9,]*(?:\.[0-9]+)?)""", RegexOption.IGNORE_CASE)
    private val FROM = Regex("""from\s*(?:\+?88)?(01[0-9]{9})""", RegexOption.IGNORE_CASE)

    fun parse(body: String): Payment? {
        val isReceived = body.contains("received", ignoreCase = true) || body.contains("পেয়েছেন")
        if (!isReceived) return null
        val trx = TRX.find(body)?.groupValues?.get(1) ?: return null
        val amt = AMT.find(body)?.groupValues?.get(1)?.replace(",", "") ?: return null
        val from = FROM.find(body)?.groupValues?.get(1) ?: "unknown"
        return Payment(trx.uppercase(Locale.US), amt, from)
    }

    fun sign(secret: String, msg: String): String {
        val mac = Mac.getInstance("HmacSHA256")
        mac.init(SecretKeySpec(secret.toByteArray(Charsets.UTF_8), "HmacSHA256"))
        return mac.doFinal(msg.toByteArray(Charsets.UTF_8)).joinToString("") { "%02x".format(it) }
    }
}
