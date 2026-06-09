import { ProcessedEvent } from '@aldabil/react-scheduler/types';
import { ScheduledTask, ScheduledTaskSchedule as ApiSchedule } from 'api-client';
import {
  addMinutes,
  addDays,
  endOfDay,
  isFriday,
  isMonday,
  isSaturday,
  isSunday,
  isThursday,
  isTuesday,
  isWednesday,
  nextFriday,
  nextMonday,
  nextSaturday,
  nextSunday,
  nextThursday,
  nextTuesday,
  nextWednesday,
} from 'date-fns';
import { getShortDescription, RecurringDays, Schedule } from 'react-components';

export const DEFAULT_CLEAN_EVENT_DURATION_MINUTES = 45;

const getPlannedEnd = (startTime: Date, plannedEndAt: string): Date => {
  const [hours, minutes] = plannedEndAt.split(':').map((value: string) => Number(value));
  const plannedEnd = new Date(startTime);
  // Interpret `plannedEndAt` as a local wall-clock time (the user's timezone when schedule was created).
  // Compute the offset between the `startTime` local hour and its UTC hour, then convert the
  // intended local hours to UTC hours so the resulting Date represents the correct instant.
  const localHour = plannedEnd.getHours();
  const utcHour = plannedEnd.getUTCHours();
  const offset = localHour - utcHour; // e.g., +8 for UTC+8
  let targetUtcHour = hours - offset;
  // Normalize to 0-23 range
  while (targetUtcHour < 0) targetUtcHour += 24;
  while (targetUtcHour >= 24) targetUtcHour -= 24;
  // Set as UTC hours so the Date's instant aligns with the original local wall-clock
  plannedEnd.setUTCHours(targetUtcHour, minutes, 0, 0);
  return plannedEnd;
};

/**
 * Generates a list of ProcessedEvents to occur within the query start and end,
 * based on the provided schedule.
 * @param start The start of the query, which is generally 00:00:00 of the first
 * day in the calendar view.
 * @param end The end of the query, which is generally 23:59:59 of the last day
 * in the calendar view.
 * @param schedule The current schedule, to be checked if there are any events
 * between start and end.
 * @param getEventId Callback function to get the event ID.
 * @param getEventTitle Callback function to get the event title.
 * @returns List of ProcessedEvents to occur within the query start and end.
 */
export const scheduleToEvents = (
  start: Date,
  end: Date,
  schedule: ApiSchedule,
  task: ScheduledTask,
  getEventId: () => number,
  getEventTitle: () => string,
): ProcessedEvent[] => {
  if (!schedule.at) {
    console.warn('Unable to convert schedule without [at] to an event');
    return [];
  }
  const [hours, minutes] = schedule.at.split(':').map((s: string) => Number(s));
  let cur = new Date(start);
  cur.setHours(hours);
  cur.setMinutes(minutes);

  const scheStartFrom = schedule.start_from ? new Date(schedule.start_from) : null;
  const scheUntil = schedule.until ? new Date(schedule.until) : null;

  let period = 8.64e7; // 1 day
  switch (schedule.period) {
    case 'day':
      break;
    case 'monday':
      cur = isMonday(cur) ? cur : nextMonday(cur);
      period *= 7;
      break;
    case 'tuesday':
      cur = isTuesday(cur) ? cur : nextTuesday(cur);
      period *= 7;
      break;
    case 'wednesday':
      cur = isWednesday(cur) ? cur : nextWednesday(cur);
      period *= 7;
      break;
    case 'thursday':
      cur = isThursday(cur) ? cur : nextThursday(cur);
      period *= 7;
      break;
    case 'friday':
      cur = isFriday(cur) ? cur : nextFriday(cur);
      period *= 7;
      break;
    case 'saturday':
      cur = isSaturday(cur) ? cur : nextSaturday(cur);
      period *= 7;
      break;
    case 'sunday':
      cur = isSunday(cur) ? cur : nextSunday(cur);
      period *= 7;
      break;
    default:
      console.warn(`Unable to convert schedule with period [${schedule.period}] to events`);
      return [];
  }

  const events: ProcessedEvent[] = [];
  while (cur <= end) {
    if (
      (scheStartFrom == null || scheStartFrom <= cur) &&
      (scheUntil == null || scheUntil >= cur)
    ) {
      const taskType = task.task_request?.category?.toLowerCase().trim();
      const curToIso = cur.toISOString();
      const curFormatted = `${curToIso.slice(0, 10)}`;
      if (!task.except_dates?.includes(curFormatted)) {
        const baseTitle = getEventTitle();
        // Try to surface schedule id when the API provides it (optional)
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const schedId = (schedule as any)?.id;
        const title = schedId != null ? `[S:${schedId}] ${baseTitle}` : baseTitle;
        let displayEnd: Date | null = null;
        let usesFallbackEnd = false;

        if (taskType === 'patrol' && schedule.planned_end_at) {
          displayEnd = getPlannedEnd(cur, schedule.planned_end_at);
          // If the planned end is before or equal to the start, roll it forward
          // to the next day until it is after start.
          while (displayEnd <= cur) {
            displayEnd = addDays(displayEnd, 1);
          }
          usesFallbackEnd = false;
        } else if (taskType === 'clean') {
          // For cleaning tasks, use actual_end_time from schedule if available or null
          // This should be the true end time after task completion
          // Here assumed actual_end_time field; fallback is null
          const actualEndIso = (schedule as any).actual_end_iso || null;
          const actualEndTimeStr = (schedule as any).actual_end_time || null;
          if (actualEndIso) {
            // Use the unambiguous ISO timestamp (UTC) if backend provided it
            displayEnd = new Date(actualEndIso);
          } else if (actualEndTimeStr) {
            displayEnd = getPlannedEnd(cur, actualEndTimeStr);
            // Debug: log actual end time values to help troubleshoot display issues
            // eslint-disable-next-line no-console
            console.debug('scheduleToEvents: clean schedule actual_end_time', {
              scheduleId: (schedule as any)?.id,
              actualEndTimeStr,
              cur: cur.toISOString(),
              displayEnd: displayEnd?.toISOString(),
            });
            // If the actual end computes to a time before the start, roll it forward
            // to the next day until it is after the start.
            while (displayEnd <= cur) {
              displayEnd = addDays(displayEnd, 1);
            }
            usesFallbackEnd = false;
          } else {
            displayEnd = null;
            usesFallbackEnd = true;
          }
        } else {
          // For other task types fallback to planned_end_at if exists
          displayEnd = schedule.planned_end_at ? getPlannedEnd(cur, schedule.planned_end_at) : null;
          if (displayEnd) {
            while (displayEnd <= cur) {
              displayEnd = addDays(displayEnd, 1);
            }
            usesFallbackEnd = false;
          } else {
            usesFallbackEnd = false;
          }
        }

        const end = displayEnd ?? addMinutes(cur, DEFAULT_CLEAN_EVENT_DURATION_MINUTES);

        events.push({
          start: cur,
          end,
          event_id: getEventId(),
          title,
          type: taskType,
          displayEnd,
          usesFallbackEnd,
        });
      }
    }

    cur = new Date(cur.valueOf() + period);
  }
  return events;
};

export const scheduleWithSelectedDay = (scheduleTask: ApiSchedule[], date: Date): Schedule => {
  const daysArray: RecurringDays = [false, false, false, false, false, false, false];

  const dayIndex = date.getDay();
  // Calculate the adjusted index to match React Scheduler (1 = Monday, ..., 7 = Sunday)
  const adjustedIndex = dayIndex === 0 ? 7 : dayIndex;

  daysArray[adjustedIndex - 1] = true;

  return {
    startOn: scheduleTask[0].start_from
      ? new Date(scheduleTask[0].start_from)
      : new Date(new Date().toUTCString()),
    days: daysArray,
    until: endOfDay(new Date(date.toISOString())),
    at: scheduleTask[0].start_from ? new Date(scheduleTask[0].start_from) : new Date(),
    plannedEndAt: scheduleTask[0].planned_end_at
      ? getPlannedEnd(
          scheduleTask[0].start_from ? new Date(scheduleTask[0].start_from) : new Date(),
          scheduleTask[0].planned_end_at,
        )
      : undefined,
    recurring: true,
  };
};

export const apiScheduleToSchedule = (scheduleTask: ApiSchedule[]): Schedule => {
  const daysOfWeek = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday'];

  const daysArray: RecurringDays = [false, false, false, false, false, false, false];

  for (const schedule of scheduleTask) {
    const dayIndex = daysOfWeek.indexOf(schedule.period.toLowerCase());
    if (dayIndex === -1) {
      throw new Error(`Invalid day: ${schedule}`);
    }

    daysArray[dayIndex] = true;
  }

  return {
    startOn: scheduleTask[0].start_from ? new Date(scheduleTask[0].start_from) : new Date(),
    days: daysArray,
    until: scheduleTask[0].until ? new Date(scheduleTask[0].until) : undefined,
    at: scheduleTask[0].start_from ? new Date(scheduleTask[0].start_from) : new Date(),
    plannedEndAt: scheduleTask[0].planned_end_at
      ? getPlannedEnd(
          scheduleTask[0].start_from ? new Date(scheduleTask[0].start_from) : new Date(),
          scheduleTask[0].planned_end_at,
        )
      : undefined,
    recurring: true,
  };
};

export const getScheduledTaskTitle = (task: ScheduledTask): string => {
  if (!task.task_request || !task.task_request.category) {
    return `[${task.id}] Unknown`;
  }
  const desc = task.task_request ? getShortDescription(task.task_request) : '';
  return `[${task.id}] ${desc}`;
};
