// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Liceron
//
// A page that holds something it cannot show again (a one-time webhook signing secret or
// delivery token) registers a guard; Studio asks before navigating away from it (Round 5
// review minor 10). The guard receives the target hash and returns true to allow navigation.

type Guard = (nextHash: string) => boolean;

let current: Guard | null = null;

export function setNavigationGuard(guard: Guard | null): void {
  current = guard;
}

/** True when navigation to `nextHash` may proceed. */
export function confirmNavigation(nextHash: string): boolean {
  return current ? current(nextHash) : true;
}
