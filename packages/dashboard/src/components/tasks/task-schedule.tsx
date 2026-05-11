import { Scheduler } from '@aldabil/react-scheduler';
import {
  CellRenderedProps,
  ProcessedEvent,
  SchedulerHelpers,
  SchedulerProps,
} from '@aldabil/react-scheduler/types';
import { Button } from '@mui/material';
import {
  ScheduledTask,
  ScheduledTaskSchedule as ApiSchedule,
  TaskFavoritePydantic as TaskFavorite,
  TaskRequest,
} from 'api-client';
import React from 'react';
import {
  ConfirmationDialog,
  CreateTaskForm,
  CreateTaskFormProps,
  EventEditDeletePopup,
  Schedule,
} from 'react-components';
import { useCreateTaskFormData } from '../../hooks/useCreateTaskForm';
import useGetUsername from '../../hooks/useFetchUser';
import { AppControllerContext } from '../app-contexts';
import { AppEvents } from '../app-events';
import { RmfAppContext } from '../rmf-app';
import { parseTasksFile, toApiSchedule } from './utils';
import {
  apiScheduleToSchedule,
  getScheduledTaskTitle,
  scheduleToEvents,
  scheduleWithSelectedDay,
} from './task-schedule-utils';

enum EventScopes {
  ALL = 'all',
  CURRENT = 'current',
}

interface CustomCalendarEditorProps {
  scheduler: SchedulerHelpers;
  value: string;
  onChange: (event: React.ChangeEvent<HTMLInputElement>) => void;
}

const disablingCellsWithoutEvents = (
  events: ProcessedEvent[],
  { start, ...props }: CellRenderedProps,
): React.ReactElement => {
  const filteredEvents = events.filter((event) => start.getTime() !== event.start.getTime());
  const disabled = filteredEvents.length > 0 || events.length === 0;
  const restProps = disabled ? {} : props;
  return (
    <Button
      style={{
        height: '100%',
        background: disabled ? '#eee' : 'transparent',
        cursor: disabled ? 'default' : 'pointer',
      }}
      disableRipple={disabled}
      {...restProps}
    />
  );
};

export const TaskSchedule = () => {
  const rmf = React.useContext(RmfAppContext);
  const { showAlert } = React.useContext(AppControllerContext);
  const { waypointNames, pickupPoints, dropoffPoints, cleaningZoneNames } =
    useCreateTaskFormData(rmf);
  const username = useGetUsername(rmf);
  const [eventScope, setEventScope] = React.useState<string>(EventScopes.CURRENT);
  const [refreshTaskAppCount, setRefreshTaskAppCount] = React.useState(0);
  const exceptDateRef = React.useRef<Date>(new Date());
  const currentEventIdRef = React.useRef<number>(-1);
  const [currentScheduleTask, setCurrentScheduledTask] = React.useState<ScheduledTask | undefined>(
    undefined,
  );
  const [calendarEvents, setCalendarEvents] = React.useState<ProcessedEvent[]>([]);
  const [openDeleteScheduleDialog, setOpenDeleteScheduleDialog] = React.useState(false);
  const [openCreateTaskForm, setOpenCreateTaskForm] = React.useState(false);
  const [scheduleToEdit, setScheduleToEdit] = React.useState<Schedule | undefined>(undefined);
  const [favoritesTasks, setFavoritesTasks] = React.useState<TaskFavorite[]>([]);
  const [isEditingSchedule, setIsEditingSchedule] = React.useState(false);

  React.useEffect(() => {
    const sub = AppEvents.refreshTaskApp.subscribe({
      next: () => {
        setRefreshTaskAppCount((oldValue) => ++oldValue);
      },
    });
    return () => sub.unsubscribe();
  }, []);

  React.useEffect(() => {
    if (!rmf) {
      return;
    }

    (async () => {
      const resp = await rmf.tasksApi.getFavoritesTasksFavoriteTasksGet();
      setFavoritesTasks(resp.data as TaskFavorite[]);
    })();

    return () => {
      setFavoritesTasks([]);
    };
  }, [rmf, refreshTaskAppCount]);

  const eventsMap = React.useRef<Record<number, ScheduledTask>>({});
  const getRemoteEvents = React.useCallback<NonNullable<SchedulerProps['getRemoteEvents']>>(
    async (params) => {
      if (!rmf) {
        return;
      }
      const tasks = (
        await rmf.tasksApi.getScheduledTasksScheduledTasksGet(
          params.end.toISOString(),
          params.start.toISOString(),
        )
      ).data;
      let counter = 0;
      const getEventId = () => {
        return counter++;
      };
      eventsMap.current = {};
      return tasks.flatMap((t: ScheduledTask) =>
        t.schedules.flatMap<ProcessedEvent>((s: ApiSchedule) => {
          const events = scheduleToEvents(params.start, params.end, s, t, getEventId, () =>
            getScheduledTaskTitle(t),
          );
          events.forEach((ev) => {
            eventsMap.current[Number(ev.event_id)] = t;
          });
          setCalendarEvents(events);
          return events;
        }),
      );
    },
    [rmf],
  );

  const CustomCalendarEditor = ({ scheduler, value, onChange }: CustomCalendarEditorProps) => {
    return (
      <ConfirmationDialog
        confirmText={'Ok'}
        cancelText="Cancel"
        open={true}
        title={'Edit recurring task'}
        submitting={undefined}
        onClose={() => {
          scheduler.close();
          setEventScope(EventScopes.CURRENT);
          AppEvents.refreshTaskApp.next();
        }}
        onSubmit={() => {
          const task = eventsMap.current[Number(currentEventIdRef.current)];
          if (!task) {
            throw new Error(`unable to find task for event ${currentEventIdRef.current}`);
          }
          setIsEditingSchedule(true);
          setCurrentScheduledTask(task);
          setScheduleToEdit(apiScheduleToSchedule(task.schedules));
          if (eventScope === EventScopes.CURRENT) {
            setScheduleToEdit(scheduleWithSelectedDay(task.schedules, exceptDateRef.current));
          }
          setOpenCreateTaskForm(true);
          AppEvents.refreshTaskApp.next();
          scheduler.close();
        }}
      >
        <EventEditDeletePopup
          currentValue={EventScopes.CURRENT}
          allValue={EventScopes.ALL}
          value={value}
          onChange={onChange}
        />
      </ConfirmationDialog>
    );
  };

  const submitTasks = React.useCallback<Required<CreateTaskFormProps>['submitTasks']>(
    async (taskRequests, schedule) => {
      if (!rmf) {
        throw new Error('tasks api not available');
      }

      if (!schedule) {
        await Promise.all(
          taskRequests.map((request) =>
            rmf.tasksApi.postDispatchTaskTasksDispatchTaskPost({
              type: 'dispatch_task_request',
              request,
            }),
          ),
        );
        AppEvents.refreshTaskApp.next();
        return;
      }

      const scheduleRequests = taskRequests.map((req) => toApiSchedule(req, schedule));

      if (isEditingSchedule && currentScheduleTask) {
        // Editing existing schedule
        let exceptDate: string | undefined = undefined;
        if (eventScope === EventScopes.CURRENT) {
          exceptDate = exceptDateRef.current.toISOString();
        }

        await Promise.all(
          scheduleRequests.map((req) =>
            rmf.tasksApi.updateScheduleTaskScheduledTasksTaskIdUpdatePost(
              currentScheduleTask.id,
              req,
              exceptDate,
            ),
          ),
        );
      } else {
        // Creating new schedule
        await Promise.all(
          scheduleRequests.map((req) => rmf.tasksApi.postScheduledTaskScheduledTasksPost(req)),
        );
      }

      setEventScope(EventScopes.CURRENT);
      AppEvents.refreshTaskApp.next();
    },
    [rmf, isEditingSchedule, currentScheduleTask, eventScope],
  );

  const submitFavoriteTask = React.useCallback<Required<CreateTaskFormProps>['submitFavoriteTask']>(
    async (taskFavoriteRequest) => {
      if (!rmf) {
        throw new Error('tasks api not available');
      }
      await rmf.tasksApi.postFavoriteTaskFavoriteTasksPost(taskFavoriteRequest);
      AppEvents.refreshTaskApp.next();
    },
    [rmf],
  );

  const deleteFavoriteTask = React.useCallback<Required<CreateTaskFormProps>['deleteFavoriteTask']>(
    async (favoriteTask) => {
      if (!rmf) {
        throw new Error('tasks api not available');
      }
      if (!favoriteTask.id) {
        throw new Error('Id is needed');
      }
      await rmf.tasksApi.deleteFavoriteTaskFavoriteTasksFavoriteTaskIdDelete(favoriteTask.id);
      AppEvents.refreshTaskApp.next();
    },
    [rmf],
  );

  const uploadFileInputRef = React.useRef<HTMLInputElement>(null);
  const tasksFromFile = React.useCallback((): Promise<TaskRequest[]> => {
    return new Promise((res) => {
      const fileInputEl = uploadFileInputRef.current;
      if (!fileInputEl) {
        return res([]);
      }

      let taskFiles: TaskRequest[];
      const listener = async () => {
        try {
          if (!fileInputEl.files || fileInputEl.files.length === 0) {
            return res([]);
          }
          try {
            taskFiles = parseTasksFile(await fileInputEl.files[0].text());
          } catch (err) {
            showAlert('error', (err as Error).message, 5000);
            return res([]);
          }
          return res(taskFiles);
        } finally {
          fileInputEl.removeEventListener('input', listener);
          fileInputEl.value = '';
        }
      };

      fileInputEl.addEventListener('input', listener);
      fileInputEl.click();
    });
  }, [showAlert]);

  const handleSubmitDeleteSchedule: React.MouseEventHandler = async (ev) => {
    ev.preventDefault();
    try {
      const task = eventsMap.current[Number(currentEventIdRef.current)];

      if (!task) {
        throw new Error(`unable to find task for event ${currentEventIdRef.current}`);
      }
      if (!rmf) {
        throw new Error('tasks api not available');
      }

      if (eventScope === EventScopes.CURRENT) {
        await rmf.tasksApi.delScheduledTasksEventScheduledTasksTaskIdClearPut(
          task.id,
          exceptDateRef.current.toISOString(),
        );
      } else {
        await rmf.tasksApi.delScheduledTasksScheduledTasksTaskIdDelete(task.id);
      }

      AppEvents.refreshTaskApp.next();

      // Set the default values
      setOpenDeleteScheduleDialog(false);
      currentEventIdRef.current = -1;
      setEventScope(EventScopes.CURRENT);
    } catch (e) {
      console.error(`Failed to delete scheduled task: ${e}`);
    }
  };

  return (
    <>
      <Button
        variant="contained"
        sx={{ mb: 2 }}
        onClick={() => {
          setIsEditingSchedule(false);
          setCurrentScheduledTask(undefined);
          setScheduleToEdit(undefined);
          setOpenCreateTaskForm(true);
        }}
      >
        Add Schedule
      </Button>

      <input
        ref={uploadFileInputRef}
        type="file"
        accept="application/json"
        style={{ display: 'none' }}
      />

      <Scheduler
        // react-scheduler does not support refreshing, workaround by mounting a new instance.

        key={`scheduler-${refreshTaskAppCount}`}
        view="week"
        month={{
          weekDays: [0, 1, 2, 3, 4, 5, 6],
          weekStartOn: 1,
          startHour: 0,
          endHour: 23,
          cellRenderer: ({ start, ...props }: CellRenderedProps) =>
            disablingCellsWithoutEvents(calendarEvents, { start, ...props }),
        }}
        week={{
          weekDays: [0, 1, 2, 3, 4, 5, 6],
          weekStartOn: 1,
          startHour: 0,
          endHour: 23,
          step: 60,
          cellRenderer: ({ start, ...props }: CellRenderedProps) =>
            disablingCellsWithoutEvents(calendarEvents, { start, ...props }),
        }}
        day={{
          startHour: 0,
          endHour: 23,
          step: 60,
          cellRenderer: ({ start, ...props }: CellRenderedProps) =>
            disablingCellsWithoutEvents(calendarEvents, { start, ...props }),
        }}
        draggable={false}
        editable={true}
        getRemoteEvents={getRemoteEvents}
        onEventClick={(event: ProcessedEvent) => {
          currentEventIdRef.current = Number(event.event_id);
          exceptDateRef.current = event.start;
        }}
        onDelete={async (deletedId: number) => {
          currentEventIdRef.current = Number(deletedId);
          setOpenDeleteScheduleDialog(true);
        }}
        customEditor={(scheduler) => (
          <CustomCalendarEditor
            scheduler={scheduler}
            value={eventScope}
            onChange={(event: React.ChangeEvent<HTMLInputElement>) => {
              setEventScope(event.target.value);
              AppEvents.refreshTaskApp.next();
            }}
          />
        )}
      />
      {openCreateTaskForm && (
        <CreateTaskForm
          user={username ? username : 'unknown user'}
          patrolWaypoints={waypointNames}
          cleaningZones={cleaningZoneNames}
          pickupPoints={pickupPoints}
          dropoffPoints={dropoffPoints}
          favoritesTasks={favoritesTasks}
          scheduleToEdit={scheduleToEdit}
          requestTask={isEditingSchedule ? currentScheduleTask?.task_request : undefined}
          open={openCreateTaskForm}
          onClose={() => {
            setOpenCreateTaskForm(false);
            setIsEditingSchedule(false);
            setCurrentScheduledTask(undefined);
            setScheduleToEdit(undefined);
          }}
          submitTasks={submitTasks}
          submitFavoriteTask={submitFavoriteTask}
          deleteFavoriteTask={deleteFavoriteTask}
          tasksFromFile={tasksFromFile}
          onSuccess={() => {
            setOpenCreateTaskForm(false);
            setIsEditingSchedule(false);
            setCurrentScheduledTask(undefined);
            setScheduleToEdit(undefined);
            showAlert('success', 'Successfully created task');
          }}
          onFail={(e) => {
            showAlert('error', `Failed to create task: ${e.message}`);
          }}
          onSuccessFavoriteTask={(message) => {
            showAlert('success', message);
          }}
          onFailFavoriteTask={(e) => {
            showAlert('error', `Failed to create or delete favorite task: ${e.message}`);
          }}
          onSuccessScheduling={() => {
            setOpenCreateTaskForm(false);
            setIsEditingSchedule(false);
            setCurrentScheduledTask(undefined);
            setScheduleToEdit(undefined);
            showAlert('success', 'Successfully created schedule');
          }}
          onFailScheduling={(e) => {
            showAlert('error', `Failed to submit schedule: ${e.message}`);
          }}
        />
      )}
      {openDeleteScheduleDialog && (
        <ConfirmationDialog
          confirmText={'Ok'}
          cancelText="Cancel"
          open={openDeleteScheduleDialog}
          title={'Delete recurring event'}
          submitting={undefined}
          onClose={() => {
            setOpenDeleteScheduleDialog(false);
            setEventScope(EventScopes.CURRENT);
          }}
          onSubmit={handleSubmitDeleteSchedule}
        >
          <EventEditDeletePopup
            currentValue={EventScopes.CURRENT}
            allValue={EventScopes.ALL}
            value={eventScope}
            onChange={(event: React.ChangeEvent<HTMLInputElement>) =>
              setEventScope(event.target.value)
            }
          />
        </ConfirmationDialog>
      )}
    </>
  );
};
