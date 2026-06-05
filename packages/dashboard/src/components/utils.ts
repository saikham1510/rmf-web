import Ajv from 'ajv';
import schema from 'api-client/schema';
import { AxiosError } from 'axios';

export function getApiErrorMessage(error: unknown): string {
  const body = (error as AxiosError).response?.data as any;
  const detail = typeof body === 'object' ? body.detail : undefined;

  if (detail === 'Task is never going to run') {
    return 'Schedule could not be saved because it has no future run time.';
  }

  if (typeof detail === 'string') {
    return detail;
  }

  if (Array.isArray(detail) && detail.length > 0) {
    return 'Please check the schedule details and try again.';
  }

  return '';
}

export function getScheduleSubmitErrorMessage(error: unknown): string {
  return getApiErrorMessage(error) || 'Schedule could not be saved. Check the dates and try again.';
}

export const ajv = new Ajv();

Object.entries(schema.components.schemas).forEach(([k, v]) => {
  ajv.addSchema(v, `#/components/schemas/${k}`);
});
