package com.quarc.sport

import androidx.activity.result.ActivityResult
import androidx.health.connect.client.HealthConnectClient
import androidx.health.connect.client.PermissionController
import androidx.health.connect.client.permission.HealthPermission
import androidx.health.connect.client.records.ActiveCaloriesBurnedRecord
import androidx.health.connect.client.records.DistanceRecord
import androidx.health.connect.client.records.ElevationGainedRecord
import androidx.health.connect.client.records.ExerciseSessionRecord
import androidx.health.connect.client.records.FloorsClimbedRecord
import androidx.health.connect.client.records.HeartRateRecord
import androidx.health.connect.client.records.Record
import androidx.health.connect.client.records.RestingHeartRateRecord
import androidx.health.connect.client.records.SleepSessionRecord
import androidx.health.connect.client.records.StepsRecord
import androidx.health.connect.client.records.TotalCaloriesBurnedRecord
import androidx.health.connect.client.request.AggregateGroupByPeriodRequest
import androidx.health.connect.client.request.AggregateRequest
import androidx.health.connect.client.request.ReadRecordsRequest
import androidx.health.connect.client.time.TimeRangeFilter
import com.getcapacitor.JSArray
import com.getcapacitor.JSObject
import com.getcapacitor.Plugin
import com.getcapacitor.PluginCall
import com.getcapacitor.PluginMethod
import com.getcapacitor.annotation.ActivityCallback
import com.getcapacitor.annotation.CapacitorPlugin
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch
import java.time.Instant
import java.time.LocalDate
import java.time.LocalDateTime
import java.time.LocalTime
import java.time.Period
import java.time.ZoneId
import kotlin.reflect.KClass

/**
 * Reads the phone's Health Connect data (steps, sleep, workouts, ...) so the app can upload it to
 * the server as its own source, "health_connect". Nothing is read until the person has allowed it
 * in Health Connect's own permission screen, and nothing is calculated here: values are what
 * Health Connect reports (its aggregate calls already de-duplicate between apps and watches).
 *
 * Methods (JS: Capacitor.Plugins.HealthConnect): availability, permissions, requestAccess,
 * openHealthConnect, read.
 */
@CapacitorPlugin(name = "HealthConnect")
class HealthConnectPlugin : Plugin() {

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    // What we ask to read. The history permission lets us go back further than 30 days; it's optional.
    private val coreRecords: List<KClass<out Record>> = listOf(
        StepsRecord::class, DistanceRecord::class, ActiveCaloriesBurnedRecord::class, TotalCaloriesBurnedRecord::class,
        FloorsClimbedRecord::class, HeartRateRecord::class, RestingHeartRateRecord::class, SleepSessionRecord::class,
        ExerciseSessionRecord::class, ElevationGainedRecord::class,
    )

    private fun corePermissions(): Set<String> = coreRecords.map { HealthPermission.getReadPermission(it) }.toSet()
    private fun allPermissions(): Set<String> = corePermissions() + HISTORY_PERMISSION

    private fun client(): HealthConnectClient = HealthConnectClient.getOrCreate(context)

    private fun launch(call: PluginCall, block: suspend () -> Unit) {
        scope.launch {
            try {
                block()
            } catch (e: SecurityException) {
                call.reject("permission", e)
            } catch (e: Exception) {
                call.reject(e.message ?: "health connect error", e)
            }
        }
    }

    // ─── availability + permissions ──────────────────────────────────────────

    @PluginMethod
    fun availability(call: PluginCall) {
        val status = when (HealthConnectClient.getSdkStatus(context)) {
            HealthConnectClient.SDK_AVAILABLE -> "available"
            HealthConnectClient.SDK_UNAVAILABLE_PROVIDER_UPDATE_REQUIRED -> "update_required"
            else -> "unavailable"
        }
        call.resolve(JSObject().put("status", status))
    }

    @PluginMethod
    fun permissions(call: PluginCall) = launch(call) { resolvePermissions(call) }

    private suspend fun resolvePermissions(call: PluginCall) {
        val granted = client().permissionController.getGrantedPermissions()
        val core = corePermissions()
        call.resolve(
            JSObject()
                .put("granted", core.any { it in granted })       // at least something can be read
                .put("complete", granted.containsAll(core))        // everything we ask for was allowed
                .put("history", HISTORY_PERMISSION in granted),
        )
    }

    // (not named requestPermissions: that is a Capacitor Plugin method with another meaning)
    @PluginMethod
    fun requestAccess(call: PluginCall) {
        val intent = PermissionController.createRequestPermissionResultContract().createIntent(context, allPermissions())
        startActivityForResult(call, intent, "permissionsResult")
    }

    @ActivityCallback
    fun permissionsResult(call: PluginCall?, @Suppress("UNUSED_PARAMETER") result: ActivityResult) {
        if (call == null) return
        launch(call) { resolvePermissions(call) }
    }

    /** Health Connect's own screen (to manage what this app may read), or its Play Store page if missing. */
    @PluginMethod
    fun openHealthConnect(call: PluginCall) {
        val intent = if (HealthConnectClient.getSdkStatus(context) == HealthConnectClient.SDK_AVAILABLE) {
            android.content.Intent(HealthConnectClient.ACTION_HEALTH_CONNECT_SETTINGS)
        } else {
            android.content.Intent(android.content.Intent.ACTION_VIEW).setData(
                android.net.Uri.parse("market://details?id=com.google.android.apps.healthdata"),
            )
        }
        try {
            activity.startActivity(intent)
            call.resolve()
        } catch (e: Exception) {
            call.reject("cannot open Health Connect", e)
        }
    }

    // ─── reading ─────────────────────────────────────────────────────────────

    /** read({ days }) → { daily: [...], sleep: [...], activities: [...] } — shaped like the server's /api/health-connect/ingest. */
    @PluginMethod
    fun read(call: PluginCall) = launch(call) {
        val days = (call.getInt("days") ?: 30).coerceIn(1, 3650)
        val client = client()
        val zone = ZoneId.systemDefault()
        val today = LocalDate.now(zone)
        val firstDay = today.minusDays(days.toLong() - 1)
        val result = JSObject()
        result.put("daily", readDaily(client, firstDay, today))
        result.put("sleep", readSleep(client, firstDay, zone))
        result.put("activities", readActivities(client, firstDay, zone))
        call.resolve(result)
    }

    private suspend fun readDaily(client: HealthConnectClient, from: LocalDate, to: LocalDate): JSArray {
        val request = AggregateGroupByPeriodRequest(
            metrics = setOf(
                StepsRecord.COUNT_TOTAL, DistanceRecord.DISTANCE_TOTAL,
                ActiveCaloriesBurnedRecord.ACTIVE_CALORIES_TOTAL, TotalCaloriesBurnedRecord.ENERGY_TOTAL,
                FloorsClimbedRecord.FLOORS_CLIMBED_TOTAL,
                HeartRateRecord.BPM_MIN, HeartRateRecord.BPM_MAX, RestingHeartRateRecord.BPM_AVG,
            ),
            timeRangeFilter = TimeRangeFilter.between(
                LocalDateTime.of(from, LocalTime.MIDNIGHT), LocalDateTime.of(to.plusDays(1), LocalTime.MIDNIGHT),
            ),
            timeRangeSlicer = Period.ofDays(1),
        )
        val rows = JSArray()
        for (day in client.aggregateGroupByPeriod(request)) {
            val r = day.result
            val row = JSObject().put("date", day.startTime.toLocalDate().toString())
            var any = false
            fun put(key: String, value: Any?) {
                if (value != null) { row.put(key, value); any = true }
            }
            put("steps", r[StepsRecord.COUNT_TOTAL])
            put("distance_meters", r[DistanceRecord.DISTANCE_TOTAL]?.inMeters)
            put("active_calories", r[ActiveCaloriesBurnedRecord.ACTIVE_CALORIES_TOTAL]?.inKilocalories?.toLong())
            put("total_calories", r[TotalCaloriesBurnedRecord.ENERGY_TOTAL]?.inKilocalories?.toLong())
            put("floors_ascended", r[FloorsClimbedRecord.FLOORS_CLIMBED_TOTAL]?.toLong())
            put("min_hr_day", r[HeartRateRecord.BPM_MIN])
            put("max_hr_day", r[HeartRateRecord.BPM_MAX])
            put("resting_hr", r[RestingHeartRateRecord.BPM_AVG])
            if (any) rows.put(row)       // days with nothing recorded are simply not sent
        }
        return rows
    }

    /** One sleep entry per day (the wake-up date): the longest session, so a nap doesn't replace a night. */
    private suspend fun readSleep(client: HealthConnectClient, from: LocalDate, zone: ZoneId): JSArray {
        val filter = TimeRangeFilter.between(from.atStartOfDay(zone).toInstant(), Instant.now())
        val best = HashMap<LocalDate, SleepSessionRecord>()
        var token: String? = null
        do {
            val page = client.readRecords(ReadRecordsRequest(SleepSessionRecord::class, filter, pageSize = 500, pageToken = token))
            for (rec in page.records) {
                val date = rec.endTime.atZone(zone).toLocalDate()
                val current = best[date]
                if (current == null || length(rec) > length(current)) best[date] = rec
            }
            token = page.pageToken
        } while (token != null)

        val rows = JSArray()
        for ((date, rec) in best.toSortedMap()) {
            var light = 0L; var deep = 0L; var rem = 0L; var awake = 0L
            for (s in rec.stages) {
                val secs = s.endTime.epochSecond - s.startTime.epochSecond
                when (s.stage) {
                    SleepSessionRecord.STAGE_TYPE_LIGHT, SleepSessionRecord.STAGE_TYPE_SLEEPING -> light += secs
                    SleepSessionRecord.STAGE_TYPE_DEEP -> deep += secs
                    SleepSessionRecord.STAGE_TYPE_REM -> rem += secs
                    SleepSessionRecord.STAGE_TYPE_AWAKE, SleepSessionRecord.STAGE_TYPE_AWAKE_IN_BED,
                    SleepSessionRecord.STAGE_TYPE_OUT_OF_BED -> awake += secs
                }
            }
            val total = length(rec)
            val hasStages = rec.stages.isNotEmpty()
            val row = JSObject()
                .put("date", date.toString())
                .put("start_time", rec.startTime.toString())
                .put("end_time", rec.endTime.toString())
                .put("duration_seconds", if (hasStages) total - awake else total)   // time asleep = session minus awake time
            if (hasStages) {
                row.put("light_seconds", light).put("deep_seconds", deep).put("rem_seconds", rem).put("awake_seconds", awake)
            }
            rows.put(row)
        }
        return rows
    }

    private fun length(rec: SleepSessionRecord): Long = rec.endTime.epochSecond - rec.startTime.epochSecond

    private suspend fun readActivities(client: HealthConnectClient, from: LocalDate, zone: ZoneId): JSArray {
        val filter = TimeRangeFilter.between(from.atStartOfDay(zone).toInstant(), Instant.now())
        val rows = JSArray()
        var token: String? = null
        do {
            val page = client.readRecords(ReadRecordsRequest(ExerciseSessionRecord::class, filter, pageSize = 200, pageToken = token))
            for (rec in page.records) {
                val totals = client.aggregate(
                    AggregateRequest(
                        metrics = setOf(
                            DistanceRecord.DISTANCE_TOTAL, ActiveCaloriesBurnedRecord.ACTIVE_CALORIES_TOTAL,
                            TotalCaloriesBurnedRecord.ENERGY_TOTAL,
                            HeartRateRecord.BPM_AVG, HeartRateRecord.BPM_MAX, ElevationGainedRecord.ELEVATION_GAINED_TOTAL,
                        ),
                        timeRangeFilter = TimeRangeFilter.between(rec.startTime, rec.endTime),
                    ),
                )
                val row = JSObject()
                    .put("record_id", rec.metadata.id)
                    .put("start_time", rec.startTime.toString())
                    .put("duration_seconds", rec.endTime.epochSecond - rec.startTime.epochSecond)
                    .put("activity_type", exerciseKey(rec.exerciseType))
                rec.title?.takeIf { it.isNotBlank() }?.let { row.put("name", it) }
                totals[DistanceRecord.DISTANCE_TOTAL]?.inMeters?.let { row.put("distance_meters", it) }
                (totals[TotalCaloriesBurnedRecord.ENERGY_TOTAL] ?: totals[ActiveCaloriesBurnedRecord.ACTIVE_CALORIES_TOTAL])
                    ?.inKilocalories?.let { row.put("calories", it.toLong()) }
                totals[HeartRateRecord.BPM_AVG]?.let { row.put("avg_hr", it) }
                totals[HeartRateRecord.BPM_MAX]?.let { row.put("max_hr", it) }
                totals[ElevationGainedRecord.ELEVATION_GAINED_TOTAL]?.inMeters?.let { row.put("elevation_gain_m", it) }
                rows.put(row)
            }
            token = page.pageToken
        } while (token != null)
        return rows
    }

    // Health Connect's exercise types → the activity keys the app already knows and has translations for.
    private fun exerciseKey(type: Int): String = when (type) {
        ExerciseSessionRecord.EXERCISE_TYPE_RUNNING -> "RUNNING"
        ExerciseSessionRecord.EXERCISE_TYPE_RUNNING_TREADMILL -> "TREADMILL_RUNNING"
        ExerciseSessionRecord.EXERCISE_TYPE_WALKING -> "WALKING"
        ExerciseSessionRecord.EXERCISE_TYPE_HIKING -> "HIKING"
        ExerciseSessionRecord.EXERCISE_TYPE_BIKING -> "CYCLING"
        ExerciseSessionRecord.EXERCISE_TYPE_BIKING_STATIONARY -> "INDOOR_CYCLING"
        ExerciseSessionRecord.EXERCISE_TYPE_SWIMMING_POOL -> "LAP_SWIMMING"
        ExerciseSessionRecord.EXERCISE_TYPE_SWIMMING_OPEN_WATER -> "OPEN_WATER_SWIMMING"
        ExerciseSessionRecord.EXERCISE_TYPE_YOGA -> "YOGA"
        ExerciseSessionRecord.EXERCISE_TYPE_STRENGTH_TRAINING -> "STRENGTH_TRAINING"
        ExerciseSessionRecord.EXERCISE_TYPE_WEIGHTLIFTING -> "WEIGHTLIFTING"
        ExerciseSessionRecord.EXERCISE_TYPE_ROWING -> "ROWING"
        ExerciseSessionRecord.EXERCISE_TYPE_ROWING_MACHINE -> "ROWING_MACHINE"
        ExerciseSessionRecord.EXERCISE_TYPE_ELLIPTICAL -> "ELLIPTICAL"
        ExerciseSessionRecord.EXERCISE_TYPE_STAIR_CLIMBING -> "STAIR_CLIMBING"
        ExerciseSessionRecord.EXERCISE_TYPE_PILATES -> "PILATES"
        ExerciseSessionRecord.EXERCISE_TYPE_HIGH_INTENSITY_INTERVAL_TRAINING -> "HIIT"
        ExerciseSessionRecord.EXERCISE_TYPE_CALISTHENICS -> "CALISTHENICS"
        ExerciseSessionRecord.EXERCISE_TYPE_SKIING -> "SKIING"
        ExerciseSessionRecord.EXERCISE_TYPE_SNOWBOARDING -> "SNOWBOARD"
        ExerciseSessionRecord.EXERCISE_TYPE_TENNIS -> "TENNIS"
        else -> "WORKOUT"
    }

    companion object {
        // "Read health data older than 30 days" — optional, granted separately by the person.
        const val HISTORY_PERMISSION = "android.permission.health.READ_HEALTH_DATA_HISTORY"
    }
}
