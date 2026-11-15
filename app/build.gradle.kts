plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.bkashbridge"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.bkashbridge"
        minSdk = 24
        targetSdk = 34
        versionCode = 1
        versionName = "1.0"
    }

    // Fixed key, so every new build installs as an UPDATE over the old one.
    signingConfigs {
        create("bridge") {
            storeFile = file("bridge.keystore")
            storePassword = "bridge123"
            keyAlias = "bridge"
            keyPassword = "bridge123"
        }
    }

    buildTypes {
        debug {
            signingConfig = signingConfigs.getByName("bridge")
        }
        release {
            isMinifyEnabled = false
            signingConfig = signingConfigs.getByName("bridge")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
}

dependencies {
    implementation("androidx.work:work-runtime-ktx:2.9.0")
}
