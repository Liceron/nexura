import { describe, expect, it } from 'vitest';
import { act, fireEvent, render, screen } from '@testing-library/react';
import { useRef, useState } from 'react';
import { useAfterRender } from '../../src/app/useAfterRender';

function Opener() {
  const [open, setOpen] = useState(false);
  const heading = useRef<HTMLHeadingElement>(null);
  const afterRender = useAfterRender();
  return (
    <>
      <button onClick={() => { setOpen(true); afterRender(() => heading.current?.focus()); }}>Open</button>
      {open && <h2 ref={heading} tabIndex={-1}>Detail</h2>}
    </>
  );
}

describe('useAfterRender', () => {
  it('focuses an element that only exists after the state change renders', async () => {
    render(<Opener />);
    await act(async () => { fireEvent.click(screen.getByText('Open')); });
    expect(screen.getByRole('heading', { name: 'Detail' })).toHaveFocus();
  });
});
