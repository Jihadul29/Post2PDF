package com.bkashbridge

import android.content.Context
import android.util.Log
import androidx.work.BackoffPolicy
import androidx.work.Constraints
import androidx.work.Data
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.Worker
import androidx.work.WorkerParameters
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.util.concurrent.TimeUnit

object Forwarder {
    fun isBkash(sender: String): Boolean = sender.contains("bkash", ignoreCase = true)

    /** Returns true when a payment was queued for sending. */
    fun handle(c: Context, sender: String, body: String): Boolean {
        if (!isBkash(sender) && !Store.anySender(c)) return false
        val p = Bkash.parse(body)
        if (p == null) {
            if (isBkash(sender)) Store.log(c, "bKash SMS ignored (not a 'received' message)")
            return false
        }
        Store.log(c, "SMS: ${p.amt} Tk, TrxID ${p.trx}, from ${p.from} -> sending")
        enqueue(c, "pay", p.trx, p.amt, p.from)
        return true
    }

    fun enqueue(c: Context, type: String, trx: String, amt: String, from: String) {
        val data = Data.Builder()
            .putString("type", type)
            .putString("trx", trx)
            .putString("amt", amt)
            .putString("from", from)
            .build()
        val req = OneTimeWorkRequestBuilder<SendWorker>()
            .setInputData(data)
            .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
            .setBackoffCriteria(BackoffPolicy.LINEAR, 30, TimeUnit.SECONDS)
            .build()
        val name = if (type == "pay") "pay-$trx" else "$type-${System.currentTimeMillis()}"
        WorkManager.getInstance(c).enqueueUniqueWork(name, ExistingWorkPolicy.KEEP, req)
    }
}

/** Posts one signed message to the relay. Retries by itself until it works. */
class SendWorker(ctx: Context, params: WorkerParameters) : Worker(ctx, params) {
    override fun doWork(): Result {
        val c = applicationContext
        val topic = Store.topic(c)
        val secret = Store.secret(c)
        if (topic.isBlank() || secret.isBlank()) {
            Store.log(c, "NOT SENT: open the app and save Topic + Secret first")
            return Result.failure()
        }
        val type = inputData.getString("type") ?: "pay"
        val trx = inputData.getString("trx") ?: ""
        val amt = inputData.getString("amt") ?: ""
        val from = inputData.getString("from") ?: ""
        val ts = (System.currentTimeMillis() / 1000).toString()
        val sig = Bkash.sign(secret, "$type|$trx|$amt|$from|$ts")
        val body = JSONObject()
            .put("type", type).put("trx", trx).put("amt", amt)
            .put("from", from).put("ts", ts).put("sig", sig)
            .toString()
        return try {
            val url = URL(Store.relay(c).trimEnd('/') + "/" + topic)
            val con = url.openConnection() as HttpURLConnection
            con.requestMethod = "POST"
            con.connectTimeout = 15000
            con.readTimeout = 20000
            con.doOutput = true
            con.setRequestProperty("Content-Type", "text/plain; charset=utf-8")
            con.outputStream.use { it.write(body.toByteArray(Charsets.UTF_8)) }
            val code = con.responseCode
            con.disconnect()
            if (code in 200..299) {
                Store.log(c, if (type == "pay") "SENT OK: TrxID $trx" else "Test message sent OK")
                Result.success()
            } else {
                Store.log(c, "Relay answered HTTP $code - will retry")
                if (runAttemptCount > 60) Result.failure() else Result.retry()
            }
        } catch (e: Exception) {
            Log.w("BkashBridge", "send failed", e)
            Store.log(c, "Network problem (${e.javaClass.simpleName}) - will retry")
            if (runAttemptCount > 200) Result.failure() else Result.retry()
        }
    }
}
