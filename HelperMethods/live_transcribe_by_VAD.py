"""
Live transcription using Voice Activity Detection (VAD) to trigger transcription.

Instead of transcribing on fixed time intervals with overlapping windows,
this approach accumulates audio continuously and only transcribes when:
1. Silence is detected for 2+ seconds, OR
2. A maximum duration (30s) is reached without silence

This produces clean, sentence-based transcriptions with no duplicates.
"""

import argparse
from doctest import debug
import queue
import re
import sys
import threading
import time
import json
import requests
from datetime import datetime

import numpy as np
import sounddevice as sd
from faster_whisper import WhisperModel
from silero_vad import load_silero_vad, get_speech_timestamps

# Word replacements for commonly misunderstood transcriptions
# Lookup is case-insensitive, but replacement preserves the specified casing
WORD_REPLACEMENTS = {
    "barry's": "Berries'",
    "barry": "Berries",
}

def list_devices_and_exit():
    """List all available input audio devices and exit."""
    print("Available input devices:")
    devices = sd.query_devices()
    for device_idx, device_info in enumerate(devices):
        if device_info["max_input_channels"] > 0:
            print(f"{device_idx:>3}: {device_info['name']}  | hostapi={device_info['hostapi']}  | sr={device_info.get('default_samplerate')}")
    sys.exit(0)


def find_input_device(device_name_substring: str | None) -> int:
    """
    Find an audio input device by name substring.
    
    Args:
        device_name_substring: Case-insensitive substring to match in device name.
                              If None, returns default input device.
    
    Returns:
        Device index as integer.
        
    Raises:
        RuntimeError: If no device matches the substring.
    """
    if not device_name_substring:
        return sd.default.device[0]
    
    name_substring_lower = device_name_substring.lower()
    devices = sd.query_devices()
    for device_idx, device_info in enumerate(devices):
        if device_info["max_input_channels"] > 0 and name_substring_lower in device_info["name"].lower():
            return device_idx
    
    raise RuntimeError(f"Could not find input device containing '{device_name_substring}'. "
                       f"Run with --list-devices to see options.")

def apply_word_replacements(text: str) -> str:
    """
    Apply case-insensitive word replacements to text.
    
    Args:
        text: The text to apply replacements to.
        
    Returns:
        Text with replacements applied.
    """
    result = text
    for search_term, replacement in WORD_REPLACEMENTS.items():
        # Use word boundaries to match whole words only
        # IGNORECASE flag makes the search case-insensitive
        pattern = r'\b' + re.escape(search_term) + r'\b'
        result = re.sub(pattern, replacement, result, flags=re.IGNORECASE)
    return result

def post_to_streamerbot(transcribed_text: str, url: str = "http://127.0.0.1:7475/DoAction"):
    data = {
    "action": {
        "id": "05d6af77-9ed2-4771-be77-1e666955873d",
    },
    "args": {
        "speaker": "TwigOtter",
        "message": transcribed_text
    }
    }

    response = requests.post(
        url,
        headers={'Content-Type': 'application/json'},
        data=json.dumps(data)
    )
    print("POST RESPONSE: " + str(response.status_code))

def main():
    """
    Main function: Set up audio streaming with VAD-based transcription.
    
    Audio accumulates continuously until 2 seconds of silence is detected,
    then the entire accumulated buffer is transcribed at once.
    """
    # Parse command-line arguments
    argument_parser = argparse.ArgumentParser(
        description="Live transcription using faster-whisper with VAD-triggered batching."
    )
    argument_parser.add_argument(
        "--speaker",
        default="TwigOtter",
        help="Name of the speaker (default: TwigOtter)"
    )
    argument_parser.add_argument(
        "--model", 
        default="large-v3", 
        help="faster-whisper model size or path (tiny, base, small, medium, large-v3, etc.)"
    )
    argument_parser.add_argument(
        "--device-name", 
        default=None, 
        help="Substring of input device name (e.g., 'CABLE Output'). Use --list-devices to discover."
    )
    argument_parser.add_argument(
        "--list-devices", 
        action="store_true", 
        help="List input devices and exit."
    )
    argument_parser.add_argument(
        "--sample-rate", 
        type=int, 
        default=16000, 
        help="Target sample rate for ASR (Hz)."
    )
    argument_parser.add_argument(
        "--language", 
        default="en", 
        help="Force language code (e.g., en). If omitted, model will auto-detect."
    )
    argument_parser.add_argument(
        "--compute-type", 
        default="float16", 
        help="e.g., int8, int8_float16, float16, float32 (GPU/CPU dependent)."
    )
    argument_parser.add_argument(
        "--silence-duration-ms",
        type=float,
        default=1500.0,
        help="Duration of silence (ms) to trigger transcription. Default: 1500ms (1.5 seconds)."
    )
    argument_parser.add_argument(
        "--max-buffer-seconds",
        type=float,
        default=15.0,
        help="Maximum seconds of audio to accumulate before forcing transcription. Default: 15 seconds."
    )
    argument_parser.add_argument(
        "--vad-threshold",
        type=float,
        default=0.5,
        help="VAD probability threshold (0-1). Higher = stricter (less false positives). Default: 0.5"
    )
    argument_parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable verbose debug logging."
    )
    argument_parser.add_argument(
        "--output-file",
        default="live_transcript.jsonl",
        help="Output JSONL file for transcriptions. Default: live_transcript.jsonl"
    )
    parsed_args = argument_parser.parse_args()
    debug = parsed_args.debug

    if parsed_args.list_devices:
        list_devices_and_exit()

    # Resolve input device
    try:
        input_device_idx = find_input_device(parsed_args.device_name)
    except Exception as e:
        print(str(e))
        list_devices_and_exit()

    selected_device_info = sd.query_devices(input_device_idx)
    print(f"[info] Using input device index {input_device_idx}: {selected_device_info['name']}")

    # Load ASR model
    print(f"[info] Loading faster-whisper model: {parsed_args.model} (compute_type={parsed_args.compute_type}) …")
    whisper_model = WhisperModel(parsed_args.model, compute_type=parsed_args.compute_type)
    
    # Load VAD model for silence detection
    print(f"[info] Loading Silero VAD model…")
    vad_model = load_silero_vad()

    # Audio streaming configuration
    sample_rate = parsed_args.sample_rate
    num_channels = 1   # mono for ASR
    audio_block_size = 1024  # frames per callback
    audio_dtype = "float32"
    
    # Calculate key durations in samples
    max_buffer_samples = int(sample_rate * parsed_args.max_buffer_seconds)
    vad_check_interval_samples = int(sample_rate * 0.1)  # Check VAD every 100ms
    min_audio_samples = sample_rate // 4  # At least 250ms of audio before transcribing
    
    print(f"[info] Silence threshold: {parsed_args.silence_duration_ms}ms")
    print(f"[info] Max buffer duration: {parsed_args.max_buffer_seconds}s")
    print(f"[info] VAD threshold: {parsed_args.vad_threshold}")

    # Shared state between threads
    audio_queue = queue.Queue(maxsize=100)
    accumulated_audio = np.array([], dtype=np.float32)
    accumulated_audio_lock = threading.Lock()
    
    # Flags for coordination
    thread_stop_event = threading.Event()
    transcription_needed_event = threading.Event()
    audio_to_transcribe = None
    transcription_lock = threading.Lock()
    speech_detected_in_buffer = False

    # Audio callback: enqueue blocks as they arrive
    def audio_callback(indata, frames, time_info, status):
        """
        Audio callback triggered by sounddevice for each audio block.
        Converts to mono and enqueues for the ingest thread.
        """
        if status:
            print(f"[audio] {status}", file=sys.stderr)
        
        # Convert to mono if needed
        if indata.shape[1] > 1:
            mono_audio = np.mean(indata, axis=1).astype(np.float32)
        else:
            mono_audio = indata[:, 0].astype(np.float32)
        
        try:
            audio_queue.put_nowait(mono_audio)
        except queue.Full:
            pass  # Drop if backpressure

    # Ingest thread: accumulates audio and monitors for silence
    def ingest_and_vad_loop():
        """
        Continuously accumulate audio from the queue.
        Periodically check the most recent audio with VAD to detect silence.
        When silence is detected for 2+ seconds, trigger transcription.
        Also triggers if max buffer duration is reached.
        """
        nonlocal accumulated_audio, audio_to_transcribe, speech_detected_in_buffer
        
        last_vad_check_time = time.time()
        silence_start_time = None
        check_count = 0
        
        print("[ingest] Thread started", flush=True)
        
        while not thread_stop_event.is_set():
            # Try to get audio block from queue
            try:
                audio_block = audio_queue.get(timeout=0.1)
            except queue.Empty:
                audio_block = None
            
            if audio_block is not None:
                with accumulated_audio_lock:
                    old_len = len(accumulated_audio)
                    accumulated_audio = np.concatenate([accumulated_audio, audio_block])
                    new_len = len(accumulated_audio)
                    if debug:
                        print(f"[ingest] Got {len(audio_block)} samples. Buffer: {old_len} → {new_len} samples ({new_len/sample_rate:.2f}s)", flush=True)
                    
                    # Keep buffer size bounded
                    if len(accumulated_audio) > max_buffer_samples:
                        print(f"[vad] Max buffer duration reached during audio accumulation. Forcing transcription.", flush=True)
                        with transcription_lock:
                            audio_to_transcribe = accumulated_audio.copy()
                            accumulated_audio = np.array([], dtype=np.float32)
                        transcription_needed_event.set()
                        silence_start_time = None  # Reset silence timer
                        speech_detected_in_buffer = False  # Reset speech detection flag
                        if debug:
                            print(f"[ingest] Buffer exceeded max, triggered transcription.", flush=True)
            
            # Periodically check VAD on the most recent audio chunk
            now = time.time()
            if now - last_vad_check_time >= 0.1:  # Check every 100ms
                last_vad_check_time = now
                check_count += 1
                
                with accumulated_audio_lock:
                    buffer_len = len(accumulated_audio)
                    # Need enough audio to check (at least 100ms)
                    if buffer_len > vad_check_interval_samples:
                        # Get the most recent chunk for VAD analysis
                        recent_chunk = accumulated_audio[-vad_check_interval_samples:]
                        
                        try:
                            if debug:
                                print(f"[vad-check #{check_count}] Checking {len(recent_chunk)} samples for speech (buffer: {buffer_len} samples = {buffer_len/sample_rate:.2f}s)", flush=True)
                            
                            # Use Silero VAD to check for speech (much faster than full transcription)
                            # Normalize only the recent chunk to [-1, 1] range
                            vad_audio = recent_chunk.copy()
                            max_val = abs(vad_audio).max()
                            if max_val > 0:
                                vad_audio = vad_audio / max_val
                            
                            if debug:
                                print(f"[vad-check #{check_count}] Audio normalized (max={max_val:.6f}, shape={vad_audio.shape})", flush=True)
                            
                            # Check if speech was detected in recent chunk
                            speech_timestamps = get_speech_timestamps(
                                vad_audio,
                                vad_model,
                                sampling_rate=sample_rate,
                                threshold=parsed_args.vad_threshold,
                                min_speech_duration_ms=50,
                                min_silence_duration_ms=50,
                                return_seconds=False
                            )
                            
                            if debug:
                                print(f"[vad-check #{check_count}] Speech timestamps: {speech_timestamps}", flush=True)
                            has_speech = len(speech_timestamps) > 0
                            if debug:
                                print(f"[vad-check #{check_count}] Has speech: {has_speech}", flush=True)
                            
                            if not has_speech:
                                # Silence detected in recent chunk
                                if silence_start_time is None:
                                    silence_start_time = now
                                    if debug:
                                        print(f"[vad-check #{check_count}] Silence started", flush=True)
                                
                                silence_duration_ms = (now - silence_start_time) * 1000
                                if debug:
                                    print(f"[vad-check #{check_count}] Silence duration: {silence_duration_ms:.0f}ms / {parsed_args.silence_duration_ms:.0f}ms threshold", flush=True)
                                
                                # Trigger transcription if silence threshold met
                                if silence_duration_ms >= parsed_args.silence_duration_ms:
                                    audio_len = len(accumulated_audio)
                                    if debug:
                                        print(f"[vad-check #{check_count}] Silence threshold met! Audio length: {audio_len} samples (need {min_audio_samples})", flush=True)
                                    
                                    if audio_len > min_audio_samples:
                                        if speech_detected_in_buffer: 
                                            # Speech was detected in buffer, transcribe it
                                            with transcription_lock:
                                                audio_to_transcribe = accumulated_audio.copy()
                                                accumulated_audio = np.array([], dtype=np.float32)
                                                if debug:
                                                    print(f"[vad-check #{check_count}] TRIGGERING TRANSCRIPTION! Copied {len(audio_to_transcribe)} samples to transcribe", flush=True)
                                            transcription_needed_event.set()
                                            print(f"[vad] Silence detected ({silence_duration_ms:.0f}ms). Triggering transcription.", flush=True)
                                            speech_detected_in_buffer = False  # Reset for next cycle
                                        else:
                                            # No speech detected, just flush the silence
                                            if debug:
                                                print(f"[vad-check #{check_count}] No speech detected, flushing accumulated silence ({audio_len} samples).", flush=True)
                                            accumulated_audio = np.array([], dtype=np.float32)
                                        silence_start_time = None  # Reset silence timer
                                    else:
                                        if debug:
                                            print(f"[vad-check #{check_count}] Silence threshold met but not enough audio yet ({audio_len} < {min_audio_samples})", flush=True)
                            else:
                                # Speech detected, reset silence timer
                                speech_detected_in_buffer = True
                                if silence_start_time is not None:
                                    if debug:
                                        print(f"[vad-check #{check_count}] Speech detected, resetting silence timer", flush=True)
                                silence_start_time = None
                        
                        except Exception as e:
                            import traceback
                            print(f"[vad] Error during VAD check: {e}", file=sys.stderr, flush=True)
                            traceback.print_exc(file=sys.stderr)
                    else:
                        if debug:
                            print(f"[vad-check #{check_count}] Not enough audio yet: {buffer_len} < {vad_check_interval_samples} samples", flush=True)

    # Start ingest thread
    ingest_thread = threading.Thread(target=ingest_and_vad_loop, daemon=True)
    ingest_thread.start()

    # Start input stream
    stream = sd.InputStream(
        device=input_device_idx,
        channels=num_channels,
        samplerate=sample_rate,
        blocksize=audio_block_size,
        dtype=audio_dtype,
        callback=audio_callback,
        latency="low",
    )

    print("[info] Starting audio stream… Press Ctrl+C to stop.")
    stream.start()

    try:
        transcription_count = 0
        while True:
            # Wait for transcription to be needed (timeout allows clean shutdown)
            if debug:
                print("[main] Waiting for transcription event…", flush=True)
            if transcription_needed_event.wait(timeout=1.0):
                transcription_needed_event.clear()
                transcription_count += 1
                print(f"\n[main] *** TRANSCRIPTION EVENT #{transcription_count} RECEIVED ***", flush=True)
                
                with transcription_lock:
                    if audio_to_transcribe is not None and len(audio_to_transcribe) > 0:
                        audio_duration_sec = len(audio_to_transcribe) / sample_rate
                        print(f"[transcribe] Processing {audio_duration_sec:.1f}s of audio…", flush=True)
                        
                        # Run Whisper on the accumulated audio
                        # Note: transcribe() returns (segments_generator, info)
                        segments_gen, _ = whisper_model.transcribe(
                            audio_to_transcribe,
                            language=parsed_args.language,
                            vad_filter=True,
                            vad_parameters=dict(
                                threshold=parsed_args.vad_threshold,
                                min_silence_duration_ms=2000
                            ),
                            beam_size=5,
                            best_of=5,
                            condition_on_previous_text=False,
                            word_timestamps=False
                        )
                        segments = list(segments_gen)
                        
                        if debug:
                            print(f"[transcribe] Whisper completed, got {len(segments)} segments", flush=True)
                        
                        # Collect all transcribed text
                        transcribed_text = " ".join(seg.text.strip() for seg in segments).strip()
                        if debug:
                            print(f"[transcribe] Combined text: {repr(transcribed_text)}", flush=True)
                        
                        if transcribed_text:
                            # Apply word replacements
                            transcribed_text = apply_word_replacements(transcribed_text)
                            if debug:
                                print(f"[transcribe] After replacements: {repr(transcribed_text)}", flush=True)
                            
                            print(f"[result] {transcribed_text}")
                            post_to_streamerbot(transcribed_text)
                            speech_detected_in_buffer = False  # Reset for next cycle
                            if debug:
                                print(f"[result] Logged to JSONL and StreamerBot", flush=True)
                        else:
                            if debug:
                                print("[result] No speech detected in accumulated audio.")
                        
                        audio_to_transcribe = None
                    else:
                        if debug:
                            print(f"[main] audio_to_transcribe is None or empty: {audio_to_transcribe is not None}, len={len(audio_to_transcribe) if audio_to_transcribe is not None else 'N/A'}", flush=True)
            else:
                if debug:
                    print("[main] Timeout waiting for transcription event (1s)", flush=True)

    except KeyboardInterrupt:
        print("\n[info] Stopping…")
    finally:
        thread_stop_event.set()
        try:
            stream.stop()
            stream.close()
        except Exception:
            pass

if __name__ == "__main__":
    main()