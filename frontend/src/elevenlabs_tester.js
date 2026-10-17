// Premium voice (ElevenLabs) via OUR backend — /v1/voice/tts and /v1/voice/stt.
//
// The ElevenLabs API key now lives only on the server (ELEVENLABS_API_KEY).
// It used to be read here from a VITE_ variable, which Vite compiles into the
// public JavaScript bundle where anyone could copy it.
//
// Both functions take `authedFetch(path, init)` (defined in main.js — it adds
// the login token + device id and does one silent token refresh on a 401) and
// THROW on any failure, exactly like the old direct calls did, so the callers'
// existing "fall back to the free voice pipeline" handling is unchanged.

// Once the server says premium voice isn't configured, stop asking for the
// rest of this page session (every attempt would just be a wasted round trip).
let serverHasNoPremiumVoice = false;

// The cross button must silence a greeting/reply that is already playing — and
// also one still downloading. stopElevenLabsAudio() pauses what is playing and
// bumps this counter, so a request that finishes AFTER the cancel doesn't start.
let ttsGeneration = 0;
let stopCurrentAudio = null;

export function stopElevenLabsAudio() {
    ttsGeneration++;
    if (stopCurrentAudio) stopCurrentAudio();
}

async function failFrom(res, what) {
    let code = "";
    try { code = (await res.json())?.error?.code || ""; } catch (_) { /* non-JSON error body */ }
    if (res.status === 503 && code === "voice_unavailable") serverHasNoPremiumVoice = true;
    throw new Error(`${what} failed (${res.status}${code ? " " + code : ""})`);
}

/**
 * Sends the recorded clip (the MediaRecorder Blob, in whatever container the
 * browser produced) to the backend, which forwards it to ElevenLabs
 * Speech-to-Text. Returns the transcribed string.
 */
export async function elevenlabsSTT(audioBlob, authedFetch) {
    if (serverHasNoPremiumVoice) throw new Error("Premium voice is not configured on the server");
    const type = (audioBlob.type || "audio/webm").split(";")[0] || "audio/webm";
    const res = await authedFetch("/v1/voice/stt", {
        method: "POST",
        headers: { "Content-Type": type },
        body: audioBlob,
    });
    if (!res.ok) await failFrom(res, "Speech-to-Text");
    const data = await res.json();
    return data.text;
}

/**
 * Asks the backend (-> ElevenLabs Text-to-Speech) for an MP3 of `text` and
 * plays it. Returns a Promise that resolves when the audio finishes playing.
 */
export async function elevenlabsTTS(text, authedFetch) {
    if (serverHasNoPremiumVoice) throw new Error("Premium voice is not configured on the server");
    const generation = ttsGeneration;
    const res = await authedFetch("/v1/voice/tts", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
    });
    if (!res.ok) await failFrom(res, "Text-to-Speech");

    const audioBlob = await res.blob();
    if (generation !== ttsGeneration) return;   // cancelled while downloading: stay silent
    const audioUrl = URL.createObjectURL(audioBlob);
    const audio = new Audio(audioUrl);

    return new Promise((resolve, reject) => {
        const cleanup = () => {
            URL.revokeObjectURL(audioUrl);
            if (stopCurrentAudio === stop) stopCurrentAudio = null;
        };
        const stop = () => {
            audio.onended = null;
            audio.onerror = null;
            audio.pause();
            cleanup();
            resolve();   // lets the awaiting voice flow carry on (or be discarded)
        };
        stopCurrentAudio = stop;
        audio.onended = () => {
            cleanup();
            resolve();
        };
        audio.onerror = (e) => {
            cleanup();
            reject(e);
        };
        audio.play().catch((e) => {
            cleanup();
            reject(e);
        });
    });
}
