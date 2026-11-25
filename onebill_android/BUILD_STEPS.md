# OneBill Android: build steps (Step 3 skeleton)

This folder is the Flutter project skeleton. The Python engine (unchanged
main.py + ui/index.html) is packed in app/app.zip.

## 1. Open in Android Studio
The android/ folder is complete now (no `flutter create` needed). In Android
Studio choose File > Open and select this folder (the one with pubspec.yaml).
Needs the Flutter and Dart plugins and Flutter 3.22 or newer. When Studio
offers "Pub get", accept it. It also creates android/local.properties with
your Flutter SDK path automatically.

## 2. Get the packages
    flutter pub get

If `flutter pub get` reports a version conflict, run `flutter pub upgrade`
and keep the package names; versions can differ slightly by Flutter version.

## 3. Run on your phone (debug)
Enable Developer options and USB debugging on the phone, connect it, then:

    flutter run

## 4. Build the APK you install
    flutter build apk --release

The file is at build/app/outputs/flutter-apk/app-release.apk. Copy it to the
phone and install it (allow "install unknown apps" once).

## Notes
- All data is stored in the app's private folder (OneBill_Data), not on
  the SD card. Use the app's backup feature regularly.
- If you change anything in python/, rebuild app/app.zip:
      cd python && zip -r ../app/app.zip main.py onebill_android_entry.py ui
- The mobile look (bottom navigation, cart sheet, card tables) is NOT in
  this step yet. That is Step 4.
- Not yet tested on a real phone. Please tell me the first error you see
  and I will fix it.

## Mobile look (Step 4)
- assets/mobile/skin.css and skin.js are injected by Flutter after each page
  load. They only change layout and style. Every tap calls the original
  button or link, so permissions and rules stay in the engine.
- Android Back closes a sheet first, then returns to Sale, then exits.
- Requires Flutter 3.22 or newer (uses PopScope). Check with `flutter --version`.
- To change the look, edit assets/mobile/skin.css and rebuild. No Python
  rebuild needed.

## EASIEST: build in the cloud (no Android Studio, no SDK download)
1. Make a free GitHub account, create a new repository, upload ALL files of this folder
   (keep the .github folder). Phone browser works.
2. Open the Actions tab > "Build OneBill APK" > Run workflow. Wait ~10-15 min.
3. Open the finished run > Artifacts > OneBill-apk. Unzip, install app-release.apk.

## NO BUILD AT ALL: run inside Termux
Copy this folder to the phone, then in Termux: bash tools/termux_run.sh
Open Chrome at http://127.0.0.1:8765 (Chrome menu > Add to Home screen).
