# Connecting Google Drive

The app reads your Takeout folder with a Google Cloud **service account**: a separate Google
identity that belongs to a project you create, and that can see only what you share with it.
Access is read-only, so the app can never change or delete anything in your Google account.

## 1. Create a Google Cloud project

1. Open the [Google Cloud console](https://console.cloud.google.com/) and sign in. Any Google
   account works; it does not have to be the one with your photos.
2. Create a project, for example *Googich Takeaway*. No billing is needed.
3. Open **APIs & Services → Library**, find **Google Drive API**, and press **Enable**.

## 2. Create the service account and its key

1. Open **IAM & Admin → Service Accounts** and press **Create service account**.
2. Give it a name, for example `googich`. It needs **no roles**: skip the permission steps.
3. Open the new account, go to its **Keys** tab, and choose **Add key → Create new key → JSON**.
   A key file downloads.

Treat the key file like a password. You upload it to the app in the next step, then delete your
downloaded copy.

If Google says key creation is disabled, your account belongs to a Google Cloud organisation that
blocks service account keys (the `iam.disableServiceAccountKeyCreation` policy). Use a project
under a personal Google account, or ask the organisation's administrator.

## 3. Add the source in the app

1. In Google Drive, open the **Takeout** folder and copy its ID: the last part of its address,
   after `/folders/`.
2. In **Configuration → Sources**, under *Add a Google Drive folder*, enter a name, the folder ID,
   and the key file. Press **Add**.

The key is encrypted before it is stored, and never shown again. The Sources page shows the
service account's address (it ends in `iam.gserviceaccount.com`).

## 4. Share the folder with the service account

1. In Google Drive, right-click the Takeout folder and choose **Share**.
2. Add the service account's address as **Viewer**, and untick *Notify people*.
3. Keep *General access* set to **Restricted**.

Back in the app, press **Test** on the source. It shows the folder's name and how many archives it
can see. The folder's name is also shown on the Sources page from then on.

## Replacing the key

To rotate the key, create a new one on the service account's Keys tab, upload it with **Replace
key** on the Sources page, then delete the old key in the Google Cloud console.

## Several Google accounts

Add one Drive source per Takeout folder. One service account can read several folders, from
different Google accounts, as long as each is shared with it.
