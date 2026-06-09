describe('scheduleToEvents with actual_end_time', () => {
  it('should use actual_end_time for clean tasks if specified', () => {
    const start = new Date('2026-06-08T00:00:00.000Z');
    const end = new Date('2026-06-09T00:00:00.000Z');
    const task = {
      id: 3,
      created_by: 'test',
      task_request: { category: 'clean', description: { zone: 'clean_inno_room' } },
      schedules: [],
    };
    const schedule = {
      id: 13,
      period: 'day',
      at: '14:00',
      start_from: '2026-06-08T00:00:00.000Z',
      actual_end_time: '14:30',
      planned_end_at: null,
      dispatched: false,
    };

    const events = scheduleToEvents(
      start,
      end,
      schedule as never,
      task as never,
      () => 1,
      () => '[3] [Clean] zone [clean_inno_room]',
    );

    expect(events).toHaveLength(1);
    expect(events[0].type).toBe('clean');
    expect(events[0].displayEnd).not.toBeNull();
    expect(events[0].usesFallbackEnd).toBe(false);
    expect(events[0].end.getHours()).toBe(14);
    expect(events[0].end.getMinutes()).toBe(30);
  });
});
import { DEFAULT_CLEAN_EVENT_DURATION_MINUTES, scheduleToEvents } from '../task-schedule-utils';

describe('scheduleToEvents', () => {
  it('renders scheduled clean tasks with a fallback visual end but no display end', () => {
    const start = new Date('2026-06-08T00:00:00.000Z');
    const end = new Date('2026-06-09T00:00:00.000Z');
    const task = {
      id: 1,
      created_by: 'test',
      task_request: { category: 'clean', description: { zone: 'clean_inno_room' } },
      schedules: [],
    };
    const schedule = {
      id: 11,
      period: 'day',
      at: '14:38',
      start_from: '2026-06-08T00:00:00.000Z',
      planned_end_at: null,
      dispatched: false,
    };

    const events = scheduleToEvents(
      start,
      end,
      schedule as never,
      task as never,
      () => 1,
      () => '[1] [Clean] zone [clean_inno_room]',
    );

    expect(events).toHaveLength(1);
    expect(events[0].type).toBe('clean');
    expect(events[0].displayEnd).toBeNull();
    expect(events[0].usesFallbackEnd).toBe(true);
    expect(events[0].end.getTime() - events[0].start.getTime()).toBe(
      DEFAULT_CLEAN_EVENT_DURATION_MINUTES * 60 * 1000,
    );
  });

  it('uses the scheduled end time for patrol tasks', () => {
    const start = new Date('2026-06-08T00:00:00.000Z');
    const end = new Date('2026-06-09T00:00:00.000Z');
    const task = {
      id: 2,
      created_by: 'test',
      task_request: { category: 'patrol', description: { places: ['wp1', 'wp2'] } },
      schedules: [],
    };
    const schedule = {
      id: 12,
      period: 'day',
      at: '14:38',
      start_from: '2026-06-08T00:00:00.000Z',
      planned_end_at: '15:08',
      dispatched: false,
    };

    const events = scheduleToEvents(
      start,
      end,
      schedule as never,
      task as never,
      () => 1,
      () => '[2] [Patrol] wp1, wp2',
    );

    expect(events).toHaveLength(1);
    expect(events[0].type).toBe('patrol');
    expect(events[0].displayEnd.getHours()).toBe(15);
    expect(events[0].displayEnd.getMinutes()).toBe(8);
    expect(events[0].usesFallbackEnd).toBe(false);
    expect(events[0].end).toEqual(events[0].displayEnd);
  });
});
