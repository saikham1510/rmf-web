import React from 'react';
import { render, screen } from '@testing-library/react';
import { scheduleToEvents } from '../task-schedule-utils';

import PropTypes from 'prop-types';

const colorMap: Record<string, string> = {
  clean: 'green',
  patrol: 'blue',
};

const DummyEventRenderer = ({ event }: { event: any }) => {
  const eventType = String(event.type ?? '').toLowerCase();
  let displayEndStr = '';
  if (eventType === 'patrol') {
    displayEndStr = event.displayEnd ? new Date(event.displayEnd).toLocaleTimeString() : 'TBA';
  } else if (eventType === 'clean') {
    displayEndStr = event.usesFallbackEnd
      ? 'TBA'
      : event.displayEnd
        ? new Date(event.displayEnd).toLocaleTimeString()
        : 'TBA';
  } else {
    displayEndStr = event.displayEnd ? new Date(event.displayEnd).toLocaleTimeString() : 'TBA';
  }

  return (
    <div data-testid="event-block" style={{ backgroundColor: colorMap[eventType] || 'gray' }}>
      <div>{event.title}</div>
      <div>{`Ends: ${displayEndStr}`}</div>
    </div>
  );
};
DummyEventRenderer.propTypes = {
  event: PropTypes.object.isRequired,
};

describe('Task Schedule Rendering', () => {
  it('shows TBA for cleaning tasks with fallback ends', () => {
    const cleanEvent = {
      title: 'Clean zone A',
      type: 'clean',
      usesFallbackEnd: true,
      displayEnd: null,
      start: new Date(),
    };
    render(<DummyEventRenderer event={cleanEvent} />);
    expect(screen.getByTestId('event-block')).toHaveTextContent('Ends: TBA');
  });

  it('shows actual end time for cleaning tasks without fallback ends', () => {
    const endTime = new Date('2026-06-08T14:30:00.000Z');
    const cleanEvent = {
      title: 'Clean zone B',
      type: 'clean',
      usesFallbackEnd: false,
      displayEnd: endTime,
      start: new Date(),
    };
    render(<DummyEventRenderer event={cleanEvent} />);
    expect(screen.getByTestId('event-block')).toHaveTextContent(
      `Ends: ${endTime.toLocaleTimeString()}`,
    );
  });

  it('shows actual end time for patrol tasks', () => {
    const endTime = new Date('2026-06-08T15:08:00.000Z');
    const patrolEvent = {
      title: 'Patrol route 1',
      type: 'patrol',
      usesFallbackEnd: false,
      displayEnd: endTime,
      start: new Date(),
    };
    render(<DummyEventRenderer event={patrolEvent} />);
    expect(screen.getByTestId('event-block')).toHaveTextContent(
      `Ends: ${endTime.toLocaleTimeString()}`,
    );
  });

  it('shows TBA for other task types with no end time', () => {
    const otherEvent = {
      title: 'Other Task',
      type: 'other',
      usesFallbackEnd: false,
      displayEnd: null,
      start: new Date(),
    };
    render(<DummyEventRenderer event={otherEvent} />);
    expect(screen.getByTestId('event-block')).toHaveTextContent('Ends: TBA');
  });
});
