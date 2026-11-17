/*
Copyright (c) 2025-2026 Filigran SAS

This file is part of the XTM One Enterprise Edition ("EE") and is
licensed under the XTM One Enterprise Edition License (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
*/

/**
 * useSpeechToText – Lightweight hook for speech-to-text input.
 *
 * Uses the browser Web Speech Recognition API (Chrome/Edge) to transcribe
 * spoken words into text.  Designed for short-form input (HITL responses,
 * search fields) — NOT for continuous voice conversation (the panel's voice call).
 *
 * Usage:
 *   const { isListening, isSupported, toggle } = useSpeechToText({
 *     onTranscript: (text) => setValue((prev) => prev + text),
 *   });
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { SpeechRecognitionAPI } from "@/lib/constants";

interface UseSpeechToTextOptions {
  /** Called with each final transcript chunk. Append to your input state. */
  onTranscript: (text: string) => void;
  /** BCP-47 language tag. Defaults to navigator.language. */
  lang?: string;
}

interface UseSpeechToTextReturn {
  /** Whether the browser supports speech recognition. */
  isSupported: boolean;
  /** Whether the microphone is currently active. */
  isListening: boolean;
  /** Start listening. No-op if already listening or unsupported. */
  start: () => void;
  /** Stop listening. No-op if not listening. */
  stop: () => void;
  /** Toggle listening on/off. */
  toggle: () => void;
}

export function useSpeechToText({
  onTranscript,
  lang,
}: UseSpeechToTextOptions): UseSpeechToTextReturn {
  const [isListening, setIsListening] = useState(false);
  const recognitionRef = useRef<SpeechRecognition | null>(null);
  const onTranscriptRef = useRef(onTranscript);
  useEffect(() => {
    onTranscriptRef.current = onTranscript;
  });

  const stop = useCallback(() => {
    const rec = recognitionRef.current;
    if (rec) {
      rec._shouldRestart = false;
      try {
        rec.stop();
      } catch {
        /* already stopped */
      }
      recognitionRef.current = null;
    }
    setIsListening(false);
  }, []);

  const start = useCallback(() => {
    if (!SpeechRecognitionAPI || recognitionRef.current) return;

    const recognition = new SpeechRecognitionAPI();
    recognition.continuous = true;
    recognition.interimResults = false;
    recognition.lang = lang || navigator.language || "en-US";

    recognition.onresult = (event: SpeechRecognitionEvent) => {
      let text = "";
      for (let i = event.resultIndex; i < event.results.length; i++) {
        if (event.results[i].isFinal) {
          text += event.results[i][0].transcript;
        }
      }
      if (text) {
        onTranscriptRef.current(text);
      }
    };

    recognition.onerror = () => {
      stop();
    };

    recognition.onend = () => {
      // Only auto-restart if we intentionally kept listening
      if (recognition._shouldRestart) {
        try {
          recognition.start();
        } catch {
          stop();
        }
      } else {
        setIsListening(false);
        recognitionRef.current = null;
      }
    };

    recognition._shouldRestart = true;
    recognitionRef.current = recognition;
    setIsListening(true);

    try {
      recognition.start();
    } catch {
      stop();
    }
  }, [lang, stop]);

  const toggle = useCallback(() => {
    if (recognitionRef.current) {
      stop();
    } else {
      start();
    }
  }, [start, stop]);

  return {
    isSupported: !!SpeechRecognitionAPI,
    isListening,
    start,
    stop,
    toggle,
  };
}
