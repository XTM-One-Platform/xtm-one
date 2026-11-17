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
 * Browser speech-to-text mic button.
 *
 * Wraps the Web Speech API (`SpeechRecognition` / `webkitSpeechRecognition`)
 * behind one toggle: click to dictate, click again (or go silent) to stop.
 * Final transcripts stream out through `onTranscript` as they are recognised.
 *
 * Everything runs in the browser — no audio ever reaches the backend — and
 * the component renders NOTHING when the API is missing (Firefox without the
 * flag), so callers need no capability check of their own. The recognition
 * language follows the browser's own locale.
 */

import { useEffect, useRef, useState } from "react";
import { Mic } from "lucide-react";
import { cn } from "@/lib/utils";
import { useFormatter } from "@/i18n";

// The Web Speech API has no lib.dom types under our TS config — the minimal
// surface used here is typed locally instead of reaching for `any`.
interface SpeechRecognitionLike {
  lang: string;
  continuous: boolean;
  interimResults: boolean;
  onresult:
    | ((event: {
        resultIndex: number;
        results: ArrayLike<
          { isFinal: boolean } & ArrayLike<{ transcript: string }>
        >;
      }) => void)
    | null;
  onend: (() => void) | null;
  onerror: (() => void) | null;
  start: () => void;
  stop: () => void;
  abort: () => void;
}

type SpeechRecognitionCtor = new () => SpeechRecognitionLike;

function recognitionCtor(): SpeechRecognitionCtor | undefined {
  const w = window as unknown as {
    SpeechRecognition?: SpeechRecognitionCtor;
    webkitSpeechRecognition?: SpeechRecognitionCtor;
  };
  return w.SpeechRecognition ?? w.webkitSpeechRecognition;
}

interface SpeechMicButtonProps {
  /** Called once per FINAL recognised utterance, already trimmed. */
  onTranscript: (text: string) => void;
  className?: string;
  disabled?: boolean;
}

export function SpeechMicButton({
  onTranscript,
  className,
  disabled,
}: SpeechMicButtonProps) {
  const { t_i18n } = useFormatter();
  const [listening, setListening] = useState(false);
  const recognitionRef = useRef<SpeechRecognitionLike | null>(null);

  // A dictation must not outlive its node (drawer closed, node deleted).
  useEffect(() => () => recognitionRef.current?.abort(), []);

  const Ctor = recognitionCtor();
  if (!Ctor) return null;

  const toggle = () => {
    if (listening) {
      recognitionRef.current?.stop();
      return;
    }
    const recognition = new Ctor();
    recognition.lang = navigator.language || "en-US";
    recognition.continuous = true;
    recognition.interimResults = false;
    recognition.onresult = (event) => {
      let text = "";
      for (let i = event.resultIndex; i < event.results.length; i += 1) {
        const result = event.results[i];
        if (result.isFinal && result[0]) text += result[0].transcript;
      }
      if (text.trim()) onTranscript(text.trim());
    };
    recognition.onend = () => setListening(false);
    recognition.onerror = () => setListening(false);
    recognitionRef.current = recognition;
    try {
      recognition.start();
      setListening(true);
    } catch {
      setListening(false);
    }
  };

  return (
    <button
      type="button"
      disabled={disabled}
      onClick={toggle}
      title={listening ? t_i18n("Stop dictation") : t_i18n("Dictate (browser speech-to-text)")}
      aria-pressed={listening}
      className={cn(
        "nodrag flex h-5 w-5 items-center justify-center rounded-sm transition-colors",
        listening
          ? "animate-pulse bg-feedback-error-primary/20 text-feedback-error-primary"
          : "text-default-secondary hover:bg-elevation-hover hover:text-default-primary",
        className,
      )}
    >
      <Mic className="h-3 w-3" />
    </button>
  );
}
