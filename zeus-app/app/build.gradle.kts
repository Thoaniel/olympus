import java.io.File

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
}

// Signeringsnøkkelen ligger utenfor repoet og må beholdes for å kunne oppdatere appen på telefonen.
val keyDir = File(System.getProperty("user.home"), ".local/share/olympus-android")

android {
    namespace = "no.olympus.zeus"
    compileSdk = 36

    defaultConfig {
        applicationId = "no.olympus.zeus"
        minSdk = 30
        targetSdk = 36
        versionCode = 24
        versionName = "0.24"
    }

    signingConfigs {
        create("release") {
            storeFile = File(keyDir, "release.jks")
            storePassword = File(keyDir, "password").takeIf { it.exists() }?.readText()?.trim()
            keyAlias = "olympus"
            keyPassword = storePassword
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = true
            isShrinkResources = true
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
            signingConfig = signingConfigs.getByName("release")
        }
        debug {
            signingConfig = signingConfigs.getByName("release")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    buildFeatures { compose = true; buildConfig = true }
}

kotlin { compilerOptions { jvmTarget.set(org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17) } }

dependencies {
    val bom = platform("androidx.compose:compose-bom:2025.11.01")
    implementation(bom)
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.foundation:foundation")
    implementation("androidx.compose.material3:material3")
    implementation("androidx.activity:activity-compose:1.11.0")
    // Casting borte: telefonen finner Chromecast/Google TV på nettet den står på og sender filmen videre
    implementation("com.google.android.gms:play-services-cast-framework:22.1.0")
    implementation("androidx.mediarouter:mediarouter:1.7.0")
    implementation("androidx.work:work-runtime-ktx:2.10.1")  // bakgrunnssjekk hvert 15. min (ferdige nedlastinger)
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.10.2")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-guava:1.10.2")
    val media3 = "1.8.0"
    implementation("androidx.media3:media3-exoplayer:$media3")
    implementation("androidx.media3:media3-session:$media3")
    implementation("androidx.media3:media3-ui:$media3")
    implementation("androidx.media3:media3-datasource-okhttp:$media3")
}
