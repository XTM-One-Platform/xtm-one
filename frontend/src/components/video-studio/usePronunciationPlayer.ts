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
 * Speaks a pronunciation lexicon term as a voice-over says it:
 * `POST /video-studio/pronunciations/speak` (the same lexicon, mechanism and
 * voice cache as a narration), then plays it, keeping how each term was sent.
 */
import { useEffect, useRef, useState } from "react";

import { useToast } from "@/components/ui/toast";
import { useSpeakPronunciation } from "@/hooks/api/useVideoStudioApi";
import { useFormatter } from "@/i18n";
import type { SpokenPronunciation } from "@/types";

/**
 * *voiceover* is the video's draft voice-over (its speech model, voice and own
 * entries); `null` speaks with the platform's model and lexicon.
 */
export function usePronunciationPlayer(voiceover: Record<string, unknown> | null) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const speak = useSpeakPronunciation();
  const audio = useRef<HTMLAudioElement | null>(null);
  const [playing, setPlaying] = useState<string | null>(null);
  const [heard, setHeard] = useState<Record<string, SpokenPronunciation>>({});
  useEffect(() => () => audio.current?.pause(), []);
  const play = async (text: string) => {
    if (!text.trim()) return;
    setPlaying(text);
    try {
      const spoken = await speak.mutateAsync({ text, voiceover });
      setHeard((current) => ({ ...current, [text]: spoken }));
      audio.current?.pause();
      audio.current = new Audio(spoken.audio_url);
      audio.current.onended = () => setPlaying(null);
      await audio.current.play();
    } catch (err) {
      setPlaying(null);
      toast({ title: t_i18n("The term could not be spoken"), description: err instanceof Error ? err.message : undefined, variant: "error" });
    }
  };
  return { play, playing, heard };
}

export type PronunciationPlayer = ReturnType<typeof usePronunciationPlayer>;
