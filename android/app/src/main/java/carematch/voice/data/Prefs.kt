package carematch.voice.data

import android.content.Context
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.core.stringSetPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.map

private val Context.prefs by preferencesDataStore("identity")

private val KEY_NAME = stringPreferencesKey("name")
private val KEY_ROOM = stringPreferencesKey("room")
private val KEY_RECENT = stringSetPreferencesKey("recent_rooms")

data class Identity(val name: String, val room: String, val recent: List<String>)

/** On-device identity + recent rooms. Never backed up, never leaves the device except as join fields. */
class IdentityStore(private val context: Context) {
    suspend fun load(): Identity {
        val p = context.prefs.data.first()
        return Identity(
            p[KEY_NAME] ?: "",
            p[KEY_ROOM] ?: "",
            p[KEY_RECENT]?.sorted().orEmpty()
        )
    }

    suspend fun save(name: String, room: String) {
        context.prefs.edit { p ->
            p[KEY_NAME] = name
            p[KEY_ROOM] = room
            val updated = (p[KEY_RECENT] ?: emptySet()).toMutableSet()
            updated.add(room)
            // Keep the list bounded; drop oldest alphabetically-stable entries beyond 8.
            while (updated.size > 8) updated.remove(updated.minOrNull())
            p[KEY_RECENT] = updated
        }
    }

    fun watch() = context.prefs.data.map { p ->
        Identity(p[KEY_NAME] ?: "", p[KEY_ROOM] ?: "", p[KEY_RECENT]?.sorted().orEmpty())
    }
}
