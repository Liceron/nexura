// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Liceron
import React, { createContext, useContext, useState, useMemo, useEffect, useRef, useCallback } from 'react';
import { useStudioExtensions } from '../app/extensions';
import en from './en.json';
import hi from './hi.json';

export type Language = 'en' | 'hi';

/**
 * Studio is English-only for now (founder decision 2026-10-29). The Hindi dictionary and the
 * language controls stay in the code, switched off; a build with VITE_LANGUAGE_CHOICE=on turns
 * them back on.
 */
export function languageChoiceEnabled(): boolean {
  return import.meta.env.VITE_LANGUAGE_CHOICE === 'on';
}
export type Translate = (key: string, fallback?: string) => string;

type Dict = typeof en;

const dictionaries: Record<Language, Dict> = {
  en,
  hi: hi as unknown as Dict,
};

interface I18nContextValue {
  language: Language;
  locale: 'en-IN' | 'hi-IN';
  setLanguage: (lang: Language) => void;
  t: (key: string, fallback?: string) => string;
  formatINR: (amount: number) => string;
}

const I18nContext = createContext<I18nContextValue | null>(null);

function resolveKey(dict: Record<string, unknown>, path: string): string {
  const parts = path.split('.');
  let current: unknown = dict;
  for (const part of parts) {
    if (current && typeof current === 'object' && part in current) {
      current = (current as Record<string, unknown>)[part];
    } else {
      return path;
    }
  }
  return typeof current === 'string' ? current : path;
}

export function I18nProvider({ children }: { children: React.ReactNode }) {
  // Studio extensions may bring translations for their own keys (core keys always win).
  const extensions = useStudioExtensions();
  const [language, setLanguageState] = useState<Language>(() => {
    try {
      const saved = localStorage.getItem('nexura_locale');
      if (saved === 'hi' && languageChoiceEnabled()) return saved;
    } catch {
      // localStorage not accessible
    }
    return 'en';
  });

  const setLanguage = (lang: Language) => {
    if (lang !== 'en' && !languageChoiceEnabled()) return;
    setLanguageState(lang);
    try {
      localStorage.setItem('nexura_locale', lang);
    } catch {
      // localStorage not accessible
    }
  };

  useEffect(() => {
    document.documentElement.lang = language;
  }, [language]);

  const value = useMemo<I18nContextValue>(() => {
    const dict = dictionaries[language] as unknown as Record<string, unknown>;
    return {
      language,
      locale: language === 'hi' ? 'hi-IN' : 'en-IN',
      setLanguage,
      t: (key: string, fallback?: string) => {
        const value = resolveKey(dict, key);
        if (value !== key) return value;
        return extensions.message(language, key) ?? fallback ?? value;
      },
      formatINR: (amount: number) => {
        return new Intl.NumberFormat(language === 'hi' ? 'hi-IN' : 'en-IN', {
          style: 'currency',
          currency: 'INR',
          maximumFractionDigits: 0,
        }).format(amount);
      },
    };
  }, [language, extensions]);

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

export function useI18n(): I18nContextValue {
  const ctx = useContext(I18nContext);
  if (!ctx) {
    throw new Error('useI18n must be used within an I18nProvider');
  }
  return ctx;
}

/** Reusable controls also render in standalone SDK panels without a provider. */
export function useT(): I18nContextValue['t'] {
  const context = useContext(I18nContext);
  return context?.t ?? englishT;
}

/** Async hooks use the current language without restarting requests or clearing drafts. */
export function useLatestT(): Translate {
  const t = useT();
  const latest = useRef(t);
  latest.current = t;
  return useCallback((key, fallback) => latest.current(key, fallback), []);
}

/** Default for pure validators and standalone consumers; views pass their locale's t. */
export const englishT: Translate = (key, fallback) => {
  const value = resolveKey(en, key);
  return value === key ? fallback ?? key : value;
};

/** Standalone panels use the Studio's English locale when no provider is present. */
export function useLocale(): 'en-IN' | 'hi-IN' {
  return useContext(I18nContext)?.locale ?? 'en-IN';
}
