# -*- coding: utf-8 -*-
"""
एक बार का YouTube लॉगिन (फ़ोन पर Google Colab में चलाएँ)।
आख़िर में REFRESH TOKEN छपेगा, उसे GitHub Secret YT_REFRESH_TOKEN में डालना है।
"""
import time

import requests

cid = input("Client ID: ").strip()
secret = input("Client Secret: ").strip()

r = requests.post(
    "https://oauth2.googleapis.com/device/code",
    data={"client_id": cid, "scope": "https://www.googleapis.com/auth/youtube"},
).json()

if "user_code" not in r:
    print("गड़बड़ी:", r)
    raise SystemExit

print("\n1) यह लिंक खोलें:", r["verification_url"])
print("2) यह कोड डालें  :", r["user_code"])
print("3) जिस चैनल पर वीडियो डालने हैं, वही चैनल/अकाउंट चुनें, फिर Allow दबाएँ।")
print("\nइंतज़ार कर रहा हूँ...\n")

interval = r.get("interval", 5)
end = time.time() + r.get("expires_in", 600)
while time.time() < end:
    time.sleep(interval)
    t = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": cid,
            "client_secret": secret,
            "device_code": r["device_code"],
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        },
    ).json()
    if "refresh_token" in t:
        print("=" * 40)
        print("YT_REFRESH_TOKEN =")
        print(t["refresh_token"])
        print("=" * 40)
        print("इसे कॉपी करके GitHub Secret में डालें। किसी को दिखाएँ नहीं।")
        break
    err = t.get("error")
    if err == "slow_down":
        interval += 5
    elif err != "authorization_pending":
        print("गड़बड़ी:", t)
        break
else:
    print("समय ख़त्म हो गया, दोबारा चलाएँ।")
