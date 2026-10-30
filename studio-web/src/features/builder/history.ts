// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Liceron
export class HistoryManager<T> {
  private _past: T[] = [];
  private _present: T;
  private _future: T[] = [];
  private readonly _maxDepth: number;

  constructor(initialState: T, maxDepth: number = 30) {
    this._present = this._clone(initialState);
    this._maxDepth = maxDepth;
  }

  get current(): T {
    return this._clone(this._present);
  }

  public canUndo(): boolean {
    return this._past.length > 0;
  }

  public canRedo(): boolean {
    return this._future.length > 0;
  }

  public push(newState: T): void {
    // Avoid storing identical consecutive states
    if (JSON.stringify(newState) === JSON.stringify(this._present)) {
      return;
    }

    this._past.push(this._present);
    if (this._past.length > this._maxDepth) {
      this._past.shift();
    }
    this._present = this._clone(newState);
    this._future = [];
  }

  public undo(): T | null {
    if (!this.canUndo()) return null;

    const previous = this._past.pop()!;
    this._future.unshift(this._present);
    this._present = previous;
    return this._clone(this._present);
  }

  public redo(): T | null {
    if (!this.canRedo()) return null;

    const next = this._future.shift()!;
    this._past.push(this._present);
    this._present = next;
    return this._clone(this._present);
  }

  public reset(state: T): void {
    this._past = [];
    this._future = [];
    this._present = this._clone(state);
  }

  private _clone(val: T): T {
    return JSON.parse(JSON.stringify(val));
  }
}
