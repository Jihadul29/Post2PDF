package com.bkashbridge

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.provider.Telephony

class SmsReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Telephony.Sms.Intents.SMS_RECEIVED_ACTION) return
        try {
            val parts = Telephony.Sms.Intents.getMessagesFromIntent(intent) ?: return
            if (parts.isEmpty()) return
            val sender = parts[0].originatingAddress ?: ""
            val body = parts.joinToString("") { it.messageBody ?: "" }
            Forwarder.handle(context.applicationContext, sender, body)
        } catch (e: Exception) {
            Store.log(context.applicationContext, "Receiver error: ${e.javaClass.simpleName}")
        }
    }
}
