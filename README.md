# bKash Bridge - Android app (auto-confirm bKash payments)

The phone app reads every bKash "You have received Tk ..." SMS, signs it, and sends it to a
private channel. Your PC bot reads that channel, matches the customer's TrxID + amount, and
confirms the order by itself. Each TrxID works ONCE - after it confirms an order it is locked.

```
Customer pays 5 tk -> bKash SMS on your phone -> APK -> relay (ntfy.sh) -> bot.py on PC
Customer sends TrxID to the bot ------------------------------------------^   matches -> Confirm -> PDFs
```

## A. Build the APK on GitHub (no Android Studio needed)

1. Create a new **private** repository on github.com.
2. Upload ALL files of this folder (drag and drop in the browser works).
   Make sure the hidden folder `.github/workflows/build.yml` is uploaded too
   (easiest: use "Add file -> Upload files" and drop the whole folder contents, or use GitHub Desktop).
3. Open the **Actions** tab. The "Build APK" run starts by itself (about 4-6 minutes).
   Wait for the green tick.
4. Download the APK:
   - Actions -> click the run -> **Artifacts** -> `bKashBridge-apk` (zip, contains bKashBridge.apk), or
   - the **Releases** section on the repo front page -> `bKashBridge.apk` (easier on the phone).
5. If the run turns red: open it, copy the error text from the failed step and send it to me.

## B. Install on the phone that receives bKash SMS

1. Copy `bKashBridge.apk` to the phone and tap it. Allow "install unknown apps" if asked.
2. Open **bKash Bridge**.
3. Tap **Generate random Topic + Secret** (it saves automatically).
4. Tap **Allow SMS permission** -> Allow.
5. Android 13/14/15 only: tap **Open App settings**, open the 3-dot menu (top right) ->
   **Allow restricted settings**, then go back and allow the SMS permission again if needed.
6. Tap **Battery** and set the app to *Unrestricted / Don't optimize* (otherwise the phone may kill it).
7. Keep the SIM that receives bKash in this phone, with mobile data or Wi-Fi on.

## C. Connect the bot (PC)

1. Replace `bot.py` on the PC with the new `bot.py` from this package (keep `fb2pdf_app.py`).
2. Open `config.txt` and add / change these lines (the first 2 are shown in the app, tap "Copy the lines"):

```
SMS_TOPIC=bk-xxxxxxxxxxxxxxxxxxxx
SMS_SECRET=xxxxxxxxxxxxxxxxxxxxxxxx
BKASH_NUMBER=01737250641
MIN_LINKS=1
PRICE_PER_LINK=5
DELIVERY_TEXT=৫-১০ মিনিট
API_BASE_URL=https://tg-bot-proxy.snowai943.workers.dev/bot
```
   Delete any `NAGAD_NUMBER` line (no longer used).
3. Start the bot (`python bot.py`). It tells you in Telegram: "bKash SMS auto-verify is ON".
4. In the phone app press **Send test message to the bot**. You should get
   "Phone app is connected" in Telegram within seconds.

## D. How a normal order works now

1. Customer sends 1 or more links -> Checkout -> pays to 01737250641 (Send Money).
2. Customer sends the TrxID to the bot.
3. Bot finds the matching SMS (same TrxID, amount >= order total) -> confirms -> PDFs start.
4. That TrxID is locked forever; sending it again is refused.

Safety nets (you get a Telegram message with Confirm / Reject buttons):
- no SMS found after 10 minutes (`PAY_WAIT_MIN`)
- customer paid less than the order total
- the number the customer typed differs from the sender in the SMS

Admin commands: `/payments` shows the last bKash SMS received and which order used each.

## Notes

- The SMS text must be the normal English bKash message ("You have received Tk ... TrxID ...").
- If the phone was off/offline, WorkManager sends the queued SMS as soon as it is back online.
  Press **Re-send bKash SMS of the last 3 days** to push anything that was missed
  (the bot ignores duplicates).
- Messages are signed with your Secret, so nobody can fake a payment even if they guess the Topic.
- The relay is ntfy.sh (free, no account). To use your own server, change *Relay URL* in the app and
  `SMS_RELAY_URL` in config.txt.
- The app uses a fixed signing key, so later builds install as an update over the old app.
