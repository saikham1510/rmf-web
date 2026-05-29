import { PostScheduledTaskRequest, TaskRequest, TaskState } from 'api-client';
import { Schedule } from 'react-components';
import schema from 'api-client/dist/schema';
import { ajv } from '../utils';
/**
 * Helper to determine if a value is a real-world epoch timestamp
 * or a small simulation counter.
 */
const formatTimestamp = (millis: number): string => {
  // Threshold: ~January 1971.
  // If the number is smaller than this, it's likely "seconds/millis since start"
  if (millis < 31536000000) {
    const seconds = Math.floor(millis / 1000);
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = seconds % 60;
    return `Sim Time: ${h}h ${m}m ${s}s`;
  }
  // Otherwise, treat as real UTC date
  return new Date(millis).toISOString().replace('T', ' ').slice(0, 19);
};

export function parseTasksFile(contents: string): TaskRequest[] {
  const obj = JSON.parse(contents) as unknown[];
  if (!Array.isArray(obj)) {
    throw new Error('Expected an array of tasks');
  }

  const errIdx = obj.findIndex((req) => !ajv.validate(schema.components.schemas.TaskRequest, req));
  if (errIdx !== -1) {
    const errors = ajv.errors!;
    throw new Error(`Validation error on item ${errIdx + 1}: ${errors[0].message}`);
  }
  return obj as TaskRequest[];
}

export function downloadCsvFull(timestamp: Date, allTasks: TaskState[]) {
  const columnSeparator = ';';
  const rowSeparator = '\n';
  const keys = Object.keys(allTasks[0]);
  let csvContent = keys.join(columnSeparator) + rowSeparator;
  allTasks.forEach((task) => {
    keys.forEach((k) => {
      type TaskStateKey = keyof typeof task;
      const columnKey = k as TaskStateKey;
      const value =
        task[columnKey] === null || task[columnKey] === undefined
          ? ''
          : JSON.stringify(task[columnKey]);
      csvContent += value + columnSeparator;
    });
    csvContent += rowSeparator;
  });
  const blob = new Blob([csvContent], { type: 'text/csv;charset=utf-8;' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `${timestamp.toJSON().slice(0, 10)}_full_task_history.csv`;
  a.click();

  setTimeout(() => {
    URL.revokeObjectURL(url);
  });
}

export function downloadCsvMinimal(timestamp: Date, allTasks: TaskState[]) {
  const columnSeparator = ';';
  const rowSeparator = '\n';
  const keys = [
    'Date',
    'Requester',
    'ID',
    'Category',
    'Assignee',
    'Start Time',
    'End Time',
    'State',
  ];
  let csvContent = keys.join(columnSeparator) + rowSeparator;
  allTasks.forEach((task) => {
    // DB timestamps are FINAL real-time values. Convert directly to ISO strings.
    const requestTime = task.booking.unix_millis_request_time;
    const startTimeStr = task.unix_millis_start_time
      ? new Date(task.unix_millis_start_time).toISOString().replace('T', ' ').slice(0, 19)
      : 'unknown';

    const endTimeStr = task.unix_millis_finish_time
      ? new Date(task.unix_millis_finish_time).toISOString().replace('T', ' ').slice(0, 19)
      : 'unknown';
    const values = [
      requestTime ? new Date(requestTime).toLocaleDateString() : 'unknown', // Date
      task.booking.requester || 'unknown', // Requester
      task.booking.id, // ID
      task.category || 'unknown', // Category
      task.assigned_to?.name || 'unknown', // Assignee
      startTimeStr, // Start Time (Fixed 2026)
      endTimeStr, // End Time (Fixed 2026)
      task.status || 'unknown',
    ];
    csvContent += values.join(columnSeparator) + rowSeparator;
  });
  const blob = new Blob([csvContent], { type: 'text/csv;charset=utf-8;' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `${timestamp.toJSON().slice(0, 10)}_minimal_task_history.csv`;
  a.click();

  setTimeout(() => {
    URL.revokeObjectURL(url);
  });
}

export const toApiSchedule = (
  taskRequest: TaskRequest,
  schedule: Schedule,
): PostScheduledTaskRequest => {
  let start = new Date(schedule.startOn);
  if (start.getTime() < 31536000000) {
    start = new Date(); // Fallback to current real time
  }

  const apiSchedules: PostScheduledTaskRequest['schedules'] = [];

  // Use the picked date/time as the first eligible occurrence.
  const start_from = start.toISOString();
  const until = schedule.until?.toISOString();
  const planned_end_at = schedule.plannedEndAt
    ? `${schedule.plannedEndAt.getHours().toString().padStart(2, '0')}:${schedule.plannedEndAt
        .getMinutes()
        .toString()
        .padStart(2, '0')}`
    : undefined;
  const scheduledTaskRequest: TaskRequest = {
    ...taskRequest,
    unix_millis_earliest_start_time: start.valueOf(),
  };

  // Extract hours/minutes from local time (user picked time in their timezone)
  const localHours = start.getHours().toString().padStart(2, '0');
  const localMinutes = start.getMinutes().toString().padStart(2, '0');
  const at = `${localHours}:${localMinutes}`;
  const recurring = schedule.recurring ?? true;
  const oneOffUntil = start.toISOString();
  const scheduleUntil = recurring ? until : oneOffUntil;
  if (!recurring) {
    const periodByDay = [
      'monday',
      'tuesday',
      'wednesday',
      'thursday',
      'friday',
      'saturday',
      'sunday',
    ] as const;
    apiSchedules.push({
      period: periodByDay[start.getDay() === 0 ? 6 : start.getDay() - 1],
      start_from,
      at,
      until: scheduleUntil,
      planned_end_at,
    });
  } else {
    schedule.days[0] &&
      apiSchedules.push({ period: 'monday', start_from, at, until: scheduleUntil, planned_end_at });
    schedule.days[1] &&
      apiSchedules.push({
        period: 'tuesday',
        start_from,
        at,
        until: scheduleUntil,
        planned_end_at,
      });
    schedule.days[2] &&
      apiSchedules.push({
        period: 'wednesday',
        start_from,
        at,
        until: scheduleUntil,
        planned_end_at,
      });
    schedule.days[3] &&
      apiSchedules.push({
        period: 'thursday',
        start_from,
        at,
        until: scheduleUntil,
        planned_end_at,
      });
    schedule.days[4] &&
      apiSchedules.push({ period: 'friday', start_from, at, until: scheduleUntil, planned_end_at });
    schedule.days[5] &&
      apiSchedules.push({
        period: 'saturday',
        start_from,
        at,
        until: scheduleUntil,
        planned_end_at,
      });
    schedule.days[6] &&
      apiSchedules.push({ period: 'sunday', start_from, at, until: scheduleUntil, planned_end_at });
  }
  return {
    task_request: scheduledTaskRequest,
    schedules: apiSchedules,
  };
};
