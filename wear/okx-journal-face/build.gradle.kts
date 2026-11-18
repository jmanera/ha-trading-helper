// Watch Face Format face: resources only, no code. The layout is src/main/res/raw/watchface.xml.
plugins {
    id("com.android.application")
}

android {
    namespace = "io.github.jmanera.hatradinghelper.okxjournalface"
    compileSdk = 35

    defaultConfig {
        applicationId = "io.github.jmanera.hatradinghelper.okxjournalface"
        minSdk = 33 // Wear OS 4: Watch Face Format version 2
        targetSdk = 35
        versionCode = 1
        versionName = "0.1.0"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            // Signed with the debug key so it can be sideloaded; use your own key to publish.
            signingConfig = signingConfigs.getByName("debug")
        }
    }
}
