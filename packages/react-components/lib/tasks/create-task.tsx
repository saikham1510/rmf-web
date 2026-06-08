/**
 * FIXME(kp): Make the whole task request system task agnostic.
 * For that RMF needs to support task discovery and UI schemas https://github.com/open-rmf/rmf_api_msgs/issues/32.
 */

import UpdateIcon from '@mui/icons-material/Create';
import DeleteIcon from '@mui/icons-material/Delete';
import PlaceOutlined from '@mui/icons-material/PlaceOutlined';
import {
  Autocomplete,
  Button,
  Chip,
  Dialog,
  DialogActions,
  DialogContent,
  DialogProps,
  DialogTitle,
  Divider,
  FormControl,
  FormControlLabel,
  FormHelperText,
  Grid,
  IconButton,
  List,
  ListItem,
  ListItemIcon,
  ListItemSecondaryAction,
  ListItemText,
  MenuItem,
  Radio,
  RadioGroup,
  styled,
  TextField,
  Typography,
  useTheme,
  Menu,
} from '@mui/material';
import { DatePicker, TimePicker } from '@mui/x-date-pickers';
import type { TaskFavoritePydantic as TaskFavorite, TaskRequest } from 'api-client';
import React from 'react';
import { Loading } from '..';
import { ConfirmationDialog, ConfirmationDialogProps } from '../confirmation-dialog';
import { PositiveIntField } from '../form-inputs';

// A bunch of manually defined descriptions to avoid using `any`.
interface Payload {
  sku: string;
  quantity: number;
}

interface TaskPlace {
  place: string;
  handler: string;
  payload: Payload;
}

interface DeliveryTaskDescription {
  pickup: TaskPlace;
  dropoff: TaskPlace;
}

interface PatrolTaskDescription {
  places: string[];
  rounds: number;
}

interface CleanTaskDescription {
  zone: string;
}

type TaskDescription = DeliveryTaskDescription | PatrolTaskDescription | CleanTaskDescription;

const isNonEmptyString = (value: string): boolean => value.length > 0;
const isPositiveNumber = (value: number): boolean => value > 0;
const PRIORITY_OPTIONS = [
  { label: 'Normal', value: 0, color: '#42A5F5' },
  { label: 'Urgent', value: 1, color: '#FB8C00' },
  { label: 'Critical', value: 2, color: '#E53935' },
] as const;

const PRIORITY_DOT_SIZE = 18;
const PRIORITY_LABEL_GAP = 12;
const isPriorityValue = (value: unknown): value is number => {
  return typeof value === 'number' && PRIORITY_OPTIONS.some((option) => option.value === value);
};

const getPriorityValue = (priority: TaskRequest['priority']): number => {
  const value = (priority as Record<string, number> | undefined)?.value;
  return isPriorityValue(value) ? value : 0;
};

const getPriorityOption = (value: number) => {
  return PRIORITY_OPTIONS.find((option) => option.value === value) ?? PRIORITY_OPTIONS[0];
};

const isTaskPlaceValid = (place: TaskPlace): boolean => {
  return (
    isNonEmptyString(place.place) &&
    isNonEmptyString(place.handler) &&
    isNonEmptyString(place.payload.sku) &&
    isPositiveNumber(place.payload.quantity)
  );
};

const isDeliveryTaskDescriptionValid = (taskDescription: DeliveryTaskDescription): boolean => {
  return isTaskPlaceValid(taskDescription.pickup) && isTaskPlaceValid(taskDescription.dropoff);
};

const isPatrolTaskDescriptionValid = (taskDescription: PatrolTaskDescription): boolean => {
  // A patrol/loop needs at least one place to form a route RMF can bid on.
  if (taskDescription.places.length < 1) {
    return false;
  }
  for (const place of taskDescription.places) {
    if (place.length === 0) {
      return false;
    }
  }
  return taskDescription.rounds > 0;
};

const isPatrolTaskDescriptionSchedulable = (taskDescription: PatrolTaskDescription): boolean => {
  return isPatrolTaskDescriptionValid(taskDescription) && taskDescription.places.length > 1;
};

const isCleanTaskDescriptionValid = (taskDescription: CleanTaskDescription): boolean => {
  return taskDescription.zone.length !== 0;
};

const classes = {
  title: 'dialogue-info-value',
  selectFileBtn: 'create-task-selected-file-btn',
  taskList: 'create-task-task-list',
  selectedTask: 'create-task-selected-task',
  actionBtn: 'dialogue-action-button',
};

const StyledDialog = styled((props: DialogProps) => <Dialog {...props} />)(({ theme }) => ({
  [`& .${classes.selectFileBtn}`]: {
    marginBottom: theme.spacing(1),
  },

  [`& .${classes.taskList}`]: {
    flex: '1 1 auto',
    minHeight: 400,
    maxHeight: '50vh',
    overflow: 'auto',
  },

  [`& .${classes.selectedTask}`]: {
    background: theme.palette.action.focus,
  },

  [`& .${classes.title}`]: {
    flex: '1 1 auto',
  },

  [`& .${classes.actionBtn}`]: {
    minWidth: 80,
  },
}));

export function getShortDescription(taskRequest: TaskRequest): string {
  switch (taskRequest.category) {
    case 'clean': {
      return `[Clean] zone [${taskRequest.description.zone}]`;
    }

    case 'delivery': {
      return `[Delivery] from [${taskRequest.description.pickup.place}] to [${taskRequest.description.dropoff.place}]`;
    }

    case 'patrol': {
      const formattedPlaces = taskRequest.description.places.map((place: string) => `[${place}]`);
      return `[Patrol] [${taskRequest.description.rounds}] round/s, along ${formattedPlaces.join(
        ', ',
      )}`;
    }

    default:
      return `[Unknown] type "${taskRequest.category}"`;
  }
}

interface FormToolbarProps {
  onSelectFileClick?: React.MouseEventHandler<HTMLButtonElement>;
}

function FormToolbar({ onSelectFileClick }: FormToolbarProps) {
  return (
    <Button
      aria-label="Select File"
      className={classes.selectFileBtn}
      variant="contained"
      color="primary"
      onClick={onSelectFileClick}
    >
      Select File
    </Button>
  );
}

interface DeliveryTaskFormProps {
  taskDesc: DeliveryTaskDescription;
  pickupPoints: Record<string, string>;
  dropoffPoints: Record<string, string>;
  onChange(taskDesc: TaskDescription): void;
  allowSubmit(allow: boolean): void;
}

function DeliveryTaskForm({
  taskDesc,
  pickupPoints = {},
  dropoffPoints = {},
  onChange,
  allowSubmit,
}: DeliveryTaskFormProps) {
  const theme = useTheme();
  const onInputChange = (desc: DeliveryTaskDescription) => {
    allowSubmit(isDeliveryTaskDescriptionValid(desc));
    onChange(desc);
  };

  return (
    <Grid container spacing={theme.spacing(2)} justifyContent="center" alignItems="center">
      <Grid item xs={6}>
        <Autocomplete
          id="pickup-location"
          freeSolo
          fullWidth
          options={Object.keys(pickupPoints)}
          value={taskDesc.pickup.place}
          onChange={(_ev, newValue) => {
            const place = newValue ?? '';
            const handler =
              newValue !== null && pickupPoints[newValue] ? pickupPoints[newValue] : '';
            onInputChange({
              ...taskDesc,
              pickup: {
                ...taskDesc.pickup,
                place: place,
                handler: handler,
              },
            });
          }}
          onBlur={(ev) =>
            pickupPoints[(ev.target as HTMLInputElement).value] &&
            onInputChange({
              ...taskDesc,
              pickup: {
                ...taskDesc.pickup,
                place: (ev.target as HTMLInputElement).value,
                handler: pickupPoints[(ev.target as HTMLInputElement).value],
              },
            })
          }
          renderInput={(params) => (
            <TextField {...params} label="Pickup Location" required={true} />
          )}
        />
      </Grid>

      <Grid item xs={4}>
        <TextField
          id="pickup_sku"
          fullWidth
          label="Pickup SKU"
          value={taskDesc.pickup.payload.sku}
          required
          onChange={(ev) => {
            onInputChange({
              ...taskDesc,
              pickup: {
                ...taskDesc.pickup,
                payload: {
                  ...taskDesc.pickup.payload,
                  sku: ev.target.value,
                },
              },
            });
          }}
        />
      </Grid>

      <Grid item xs={2}>
        <PositiveIntField
          id="pickup_quantity"
          label="Quantity"
          value={taskDesc.pickup.payload.quantity}
          onChange={(_ev, val) => {
            onInputChange({
              ...taskDesc,
              pickup: {
                ...taskDesc.pickup,
                payload: {
                  ...taskDesc.pickup.payload,
                  quantity: val,
                },
              },
            });
          }}
        />
      </Grid>

      <Grid item xs={6}>
        <Autocomplete
          id="dropoff-location"
          freeSolo
          fullWidth
          options={Object.keys(dropoffPoints)}
          value={taskDesc.dropoff.place}
          onChange={(_ev, newValue) => {
            const place = newValue ?? '';
            const handler =
              newValue !== null && dropoffPoints[newValue] ? dropoffPoints[newValue] : '';
            onInputChange({
              ...taskDesc,
              dropoff: {
                ...taskDesc.dropoff,
                place: place,
                handler: handler,
              },
            });
          }}
          onBlur={(ev) =>
            dropoffPoints[(ev.target as HTMLInputElement).value] &&
            onInputChange({
              ...taskDesc,
              dropoff: {
                ...taskDesc.dropoff,
                place: (ev.target as HTMLInputElement).value,
                handler: dropoffPoints[(ev.target as HTMLInputElement).value],
              },
            })
          }
          renderInput={(params) => (
            <TextField {...params} label="Dropoff Location" required={true} />
          )}
        />
      </Grid>

      <Grid item xs={4}>
        <TextField
          id="dropoff_sku"
          fullWidth
          label="Dropoff SKU"
          value={taskDesc.dropoff.payload.sku}
          required
          onChange={(ev) => {
            onInputChange({
              ...taskDesc,
              dropoff: {
                ...taskDesc.dropoff,
                payload: {
                  ...taskDesc.dropoff.payload,
                  sku: ev.target.value,
                },
              },
            });
          }}
        />
      </Grid>

      <Grid item xs={2}>
        <PositiveIntField
          id="dropoff_quantity"
          label="Quantity"
          value={taskDesc.dropoff.payload.quantity}
          onChange={(_ev, val) => {
            onInputChange({
              ...taskDesc,
              dropoff: {
                ...taskDesc.dropoff,
                payload: {
                  ...taskDesc.dropoff.payload,
                  quantity: val,
                },
              },
            });
          }}
        />
      </Grid>
    </Grid>
  );
}

interface PlaceListProps {
  places: string[];
  onClick(places_index: number): void;
}

function PlaceList({ places, onClick }: PlaceListProps) {
  const theme = useTheme();

  return (
    <List
      dense
      sx={{
        bgcolor: 'background.paper',
        marginLeft: theme.spacing(3),
        marginRight: theme.spacing(3),
      }}
    >
      {places.map((value, index) => (
        <ListItem
          key={`${value}-${index}`}
          secondaryAction={
            <IconButton edge="end" aria-label="delete" onClick={() => onClick(index)}>
              <DeleteIcon />
            </IconButton>
          }
        >
          <ListItemIcon>
            <PlaceOutlined />
          </ListItemIcon>

          <ListItemText primary={`Place Name: ${value}`} />
        </ListItem>
      ))}
    </List>
  );
}

interface PatrolTaskFormProps {
  taskDesc: PatrolTaskDescription;
  patrolWaypoints: string[];
  onChange(patrolTaskDescription: PatrolTaskDescription): void;
  allowSubmit(allow: boolean): void;
}

function PatrolTaskForm({ taskDesc, patrolWaypoints, onChange, allowSubmit }: PatrolTaskFormProps) {
  const theme = useTheme();
  const onInputChange = (desc: PatrolTaskDescription) => {
    allowSubmit(isPatrolTaskDescriptionValid(desc));
    onChange(desc);
  };

  return (
    <Grid container spacing={theme.spacing(2)} justifyContent="center" alignItems="center">
      <Grid item xs={12}>
        <Autocomplete
          id="place-input"
          freeSolo
          fullWidth
          options={patrolWaypoints}
          onChange={(_ev, newValue) =>
            newValue !== null &&
            onInputChange({
              ...taskDesc,
              places: taskDesc.places.concat(newValue).filter((el: string) => el),
            })
          }
          renderInput={(params) => <TextField {...params} label="Place Name" required={true} />}
        />
      </Grid>

      <Grid item xs={12}>
        <PlaceList
          places={taskDesc && taskDesc.places ? taskDesc.places : []}
          onClick={(places_index) =>
            taskDesc.places.splice(places_index, 1) &&
            onInputChange({
              ...taskDesc,
            })
          }
        />
      </Grid>
    </Grid>
  );
}

interface CleanTaskFormProps {
  taskDesc: CleanTaskDescription;
  cleaningZones: string[];
  onChange(cleanTaskDescription: CleanTaskDescription): void;
  allowSubmit(allow: boolean): void;
}

function CleanTaskForm({ taskDesc, cleaningZones, onChange, allowSubmit }: CleanTaskFormProps) {
  const onInputChange = (desc: CleanTaskDescription) => {
    allowSubmit(isCleanTaskDescriptionValid(desc));
    onChange(desc);
  };

  return (
    <Autocomplete
      id="cleaning-zone"
      freeSolo
      fullWidth
      options={cleaningZones}
      value={taskDesc.zone}
      onChange={(_ev, newValue) => {
        const zone = newValue ?? '';
        onInputChange({
          ...taskDesc,

          zone: zone,
        });
      }}
      onBlur={(ev) => onInputChange({ ...taskDesc, zone: (ev.target as HTMLInputElement).value })}
      renderInput={(params) => <TextField {...params} label="Cleaning Zone" required={true} />}
    />
  );
}

interface FavoriteTaskProps {
  listItemText: string;
  listItemClick: () => void;
  favoriteTask: TaskFavorite;
  setFavoriteTask: (favoriteTask: TaskFavorite) => void;
  setOpenDialog: (open: boolean) => void;
  setCallToDelete: (open: boolean) => void;
  setCallToUpdate: (open: boolean) => void;
}

function FavoriteTask({
  listItemText,
  listItemClick,
  favoriteTask,
  setFavoriteTask,
  setOpenDialog,
  setCallToDelete,
  setCallToUpdate,
}: FavoriteTaskProps) {
  const theme = useTheme();

  return (
    <>
      <ListItem
        sx={{ width: theme.spacing(30) }}
        onClick={() => {
          listItemClick();
          setCallToUpdate(false);
        }}
        role="listitem button"
        button
        divider={true}
      >
        <ListItemText primary={listItemText} />

        <ListItemSecondaryAction>
          <IconButton
            edge="end"
            aria-label="update"
            onClick={() => {
              setCallToUpdate(true);
              listItemClick();
            }}
          >
            <UpdateIcon />
          </IconButton>

          <IconButton
            edge="end"
            aria-label="delete"
            onClick={() => {
              setOpenDialog(true);
              setFavoriteTask(favoriteTask);
              setCallToDelete(true);
            }}
          >
            <DeleteIcon />
          </IconButton>
        </ListItemSecondaryAction>
      </ListItem>
    </>
  );
}

function defaultCleanTask(): CleanTaskDescription {
  return {
    zone: '',
  };
}

function defaultPatrolTask(): PatrolTaskDescription {
  return {
    places: [],
    rounds: 1,
  };
}

function defaultDeliveryTask(): DeliveryTaskDescription {
  return {
    pickup: {
      place: '',
      handler: '',
      payload: {
        sku: '',
        quantity: 1,
      },
    },

    dropoff: {
      place: '',
      handler: '',
      payload: {
        sku: '',
        quantity: 1,
      },
    },
  };
}

function defaultTaskDescription(taskCategory: string): TaskDescription | undefined {
  switch (taskCategory) {
    case 'clean':
      return defaultCleanTask();

    case 'patrol':
      return defaultPatrolTask();

    case 'delivery':
      return defaultDeliveryTask();

    default:
      return undefined;
  }
}

function defaultTask(): TaskRequest {
  return {
    category: 'patrol',
    description: defaultPatrolTask(),
    unix_millis_earliest_start_time: 0,
    unix_millis_request_time: Date.now(),
    priority: { type: 'binary', value: 0 },
    requester: '',
  };
}

export type RecurringDays = [boolean, boolean, boolean, boolean, boolean, boolean, boolean];
export interface Schedule {
  startOn: Date;
  days: RecurringDays;
  until?: Date;
  at: Date;
  plannedEndAt?: Date;
  recurring: boolean;
}

enum ScheduleUntilValue {
  NEVER = 'never',
  ON = 'on',
}

enum ScheduleTypeValue {
  ONE_TIME = 'one-time',
  RECURRING = 'recurring',
}

interface DaySelectorSwitchProps {
  disabled?: boolean;
  onChange: (checked: RecurringDays) => void;
  value: RecurringDays;
}

const DaySelectorSwitch: React.VFC<DaySelectorSwitchProps> = ({ disabled, onChange, value }) => {
  const theme = useTheme();

  const renderChip = (idx: number, text: string) => (
    <Chip
      key={idx}
      label={text}
      color="primary"
      sx={{ '&:hover': {}, margin: theme.spacing(0.25) }}
      variant={value[idx] && !disabled ? 'filled' : 'outlined'}
      disabled={disabled}
      onClick={() => {
        value[idx] = !value[idx];

        onChange([...value]);
      }}
    />
  );

  return (
    <div>
      {renderChip(0, 'Mon')}
      {renderChip(1, 'Tue')}
      {renderChip(2, 'Wed')}
      {renderChip(3, 'Thu')}
      {renderChip(4, 'Fri')}
      {renderChip(5, 'Sat')}
      {renderChip(6, 'Sun')}
    </div>
  );
};

const defaultFavoriteTask = (): TaskFavorite => {
  return {
    id: '',
    name: '',
    category: 'patrol',
    description: defaultPatrolTask(),
    unix_millis_earliest_start_time: 0,
    priority: { type: 'binary', value: 0 },
    user: '',
  };
};

const defaultPlannedEnd = (startTime: Date): Date => {
  const plannedEnd = new Date(startTime.valueOf());
  plannedEnd.setMinutes(plannedEnd.getMinutes() + 45);
  return plannedEnd;
};

const endOfDay = (date: Date): Date => {
  const endDate = new Date(date.valueOf());
  endDate.setHours(23, 59, 0, 0);
  return endDate;
};

const isSameDay = (lhs?: Date, rhs?: Date): boolean => {
  if (!lhs || !rhs) {
    return false;
  }

  return (
    lhs.getFullYear() === rhs.getFullYear() &&
    lhs.getMonth() === rhs.getMonth() &&
    lhs.getDate() === rhs.getDate()
  );
};

const getRecurringDaysForDate = (date: Date): RecurringDays => {
  const days: RecurringDays = [false, false, false, false, false, false, false];
  const dayIndex = date.getDay() === 0 ? 6 : date.getDay() - 1;
  days[dayIndex] = true;
  return days;
};

const hasSelectedRecurringDay = (days: RecurringDays): boolean => days.some(Boolean);

const makeDefaultSchedule = (): Schedule => {
  const startOn = new Date();

  return {
    startOn,
    days: getRecurringDaysForDate(startOn),
    until: endOfDay(startOn),
    at: new Date(startOn.valueOf()),
    recurring: false,
  };
};

export interface CreateTaskFormProps
  extends Omit<ConfirmationDialogProps, 'onConfirmClick' | 'toolbar'> {
  /**
   * Shows extra UI elements suitable for submittng batched tasks. Default to 'false'.
   */
  user: string;
  allowBatch?: boolean;
  showFavorite?: boolean;
  cleaningZones?: string[];
  patrolWaypoints?: string[];
  pickupPoints?: Record<string, string>;
  dropoffPoints?: Record<string, string>;
  favoritesTasks?: TaskFavorite[];
  mode?: 'immediate' | 'full';
  scheduleToEdit?: Schedule;
  requestTask?: TaskRequest;
  submitTasks?(tasks: TaskRequest[], schedule: Schedule | null): Promise<void>;
  tasksFromFile?(): Promise<TaskRequest[]> | TaskRequest[];
  onSuccess?(tasks: TaskRequest[]): void;
  onFail?(error: Error, tasks: TaskRequest[]): void;
  onSuccessFavoriteTask?(message: string, favoriteTask: TaskFavorite): void;
  onFailFavoriteTask?(error: Error, favoriteTask: TaskFavorite): void;
  submitFavoriteTask?(favoriteTask: TaskFavorite): Promise<void>;
  deleteFavoriteTask?(favoriteTask: TaskFavorite): Promise<void>;
  onSuccessScheduling?(): void;
  onFailScheduling?(error: Error): void;
}

export function CreateTaskForm({
  user,
  cleaningZones = [],
  patrolWaypoints = [],
  pickupPoints = {},
  dropoffPoints = {},
  favoritesTasks = [],
  mode = 'full',
  scheduleToEdit,
  requestTask,
  showFavorite = false,
  submitTasks,
  tasksFromFile,
  onClose,
  onSuccess,
  onFail,
  onSuccessFavoriteTask,
  onFailFavoriteTask,
  submitFavoriteTask,
  deleteFavoriteTask,
  onSuccessScheduling,
  onFailScheduling,

  ...otherProps
}: CreateTaskFormProps): JSX.Element {
  const theme = useTheme();
  const SHOW_FAVORITES_UI = false;
  const immediateMode = mode === 'immediate';
  const defaultSchedule = React.useMemo(() => makeDefaultSchedule(), []);
  const [openFavoriteDialog, setOpenFavoriteDialog] = React.useState(false);
  const [callToDeleteFavoriteTask, setCallToDeleteFavoriteTask] = React.useState(false);
  const [callToUpdateFavoriteTask, setCallToUpdateFavoriteTask] = React.useState(false);
  const [deletingFavoriteTask, setDeletingFavoriteTask] = React.useState(false);
  const [favoriteTaskBuffer, setFavoriteTaskBuffer] =
    React.useState<TaskFavorite>(defaultFavoriteTask());
  const [favoriteTaskTitleError, setFavoriteTaskTitleError] = React.useState(false);
  const [savingFavoriteTask, setSavingFavoriteTask] = React.useState(false);
  const [taskRequests, setTaskRequests] = React.useState<TaskRequest[]>(() => [
    requestTask ?? defaultTask(),
  ]);

  const [selectedTaskIdx, setSelectedTaskIdx] = React.useState(0);
  const taskTitles = React.useMemo(
    () => taskRequests && taskRequests.map((t, i) => `${i + 1}: ${getShortDescription(t)}`),

    [taskRequests],
  );

  const [submitting, setSubmitting] = React.useState(false);
  const [formFullyFilled, setFormFullyFilled] = React.useState(requestTask !== undefined || false);
  const taskRequest = taskRequests[selectedTaskIdx];
  const isCleanTask = taskRequest.category === 'clean';
  const [openSchedulingDialog, setOpenSchedulingDialog] = React.useState(false);
  const [schedule, setSchedule] = React.useState<Schedule>(() => {
    if (immediateMode) {
      return defaultSchedule;
    }

    if (!scheduleToEdit) {
      return defaultSchedule;
    }

    const mergedSchedule = {
      ...defaultSchedule,
      ...scheduleToEdit,
      recurring: scheduleToEdit.recurring ?? true,
    };

    return mergedSchedule.recurring
      ? mergedSchedule
      : { ...mergedSchedule, until: endOfDay(mergedSchedule.startOn) };
  });

  const [scheduleUntilValue, setScheduleUntilValue] = React.useState<string>(() => {
    if (immediateMode) {
      return ScheduleUntilValue.ON;
    }

    if (!scheduleToEdit) {
      return ScheduleUntilValue.ON;
    }

    return scheduleToEdit.recurring ?? true
      ? scheduleToEdit.until
        ? ScheduleUntilValue.ON
        : ScheduleUntilValue.NEVER
      : ScheduleUntilValue.ON;
  });

  React.useEffect(() => {
    if (!isCleanTask) {
      return;
    }

    setSchedule((prev) => ({
      ...prev,
      plannedEndAt: undefined,
    }));
  }, [isCleanTask]);

  const handleScheduleUntilValue = (event: React.ChangeEvent<HTMLInputElement>) => {
    if (event.target.value === ScheduleUntilValue.ON) {
      setSchedule((prev) => ({ ...prev, until: endOfDay(prev.startOn) }));
    } else {
      setSchedule((prev) => ({ ...prev, until: undefined }));
    }
    setScheduleUntilValue(event.target.value);
  };

  // schedule is not supported with batch upload

  const scheduleEnabled = !immediateMode && taskRequests.length === 1;
  const scheduleActionEnabled =
    scheduleEnabled &&
    formFullyFilled &&
    taskRequest.category !== 'delivery' &&
    (taskRequest.category !== 'patrol' ||
      isPatrolTaskDescriptionSchedulable(taskRequest.description as PatrolTaskDescription));

  const updateTasks = () => {
    setTaskRequests((prev) => {
      prev.splice(selectedTaskIdx, 1, taskRequest);

      return [...prev];
    });
  };

  const handleTaskDescriptionChange = (newCategory: string, newDesc: TaskDescription) => {
    taskRequest.category = newCategory;
    taskRequest.description = newDesc;
    setFavoriteTaskBuffer({ ...favoriteTaskBuffer, description: newDesc, category: newCategory });
    updateTasks();
  };

  const allowSubmit = (allow: boolean) => {
    setFormFullyFilled(allow);
  };

  const renderTaskDescriptionForm = () => {
    switch (taskRequest.category) {
      case 'clean':
        return (
          <CleanTaskForm
            taskDesc={taskRequest.description as CleanTaskDescription}
            cleaningZones={cleaningZones}
            onChange={(desc) => handleTaskDescriptionChange('clean', desc)}
            allowSubmit={allowSubmit}
          />
        );

      case 'patrol':
        return (
          <PatrolTaskForm
            taskDesc={taskRequest.description as PatrolTaskDescription}
            patrolWaypoints={patrolWaypoints}
            onChange={(desc) => handleTaskDescriptionChange('patrol', desc)}
            allowSubmit={allowSubmit}
          />
        );

      case 'delivery':
        return (
          <DeliveryTaskForm
            taskDesc={taskRequest.description as DeliveryTaskDescription}
            pickupPoints={pickupPoints}
            dropoffPoints={dropoffPoints}
            onChange={(desc) => handleTaskDescriptionChange('delivery', desc)}
            allowSubmit={allowSubmit}
          />
        );

      default:
        return null;
    }
  };

  const handleTaskTypeChange = (ev: React.ChangeEvent<HTMLInputElement>) => {
    const newCategory = ev.target.value;
    const newDesc = defaultTaskDescription(newCategory);

    if (newDesc === undefined) {
      return;
    }

    taskRequest.description = newDesc;
    taskRequest.category = newCategory;

    if (newCategory !== 'clean') {
      taskRequest.unix_millis_earliest_start_time = 0;
    }

    setFavoriteTaskBuffer({
      ...favoriteTaskBuffer,
      category: newCategory,
      description: newDesc,
      unix_millis_earliest_start_time:
        newCategory === 'clean' ? favoriteTaskBuffer.unix_millis_earliest_start_time : 0,
    });
    updateTasks();
  };

  // no memo because deps would likely change

  const handleSubmit = async (scheduling: boolean) => {
    if (!submitTasks) {
      onSuccess && onSuccess(taskRequests);
      return;
    }

    const requester = scheduling ? `${user}__scheduled` : user;

    for (const t of taskRequests) {
      t.requester = requester;
      t.unix_millis_request_time = Date.now();

      if (t.category !== 'clean') {
        t.unix_millis_earliest_start_time = 0;
      }
    }

    const submittingSchedule = !immediateMode && scheduling && !!schedule;

    try {
      setSubmitting(true);
      await submitTasks(taskRequests, submittingSchedule ? schedule : null);
      setSubmitting(false);

      if (submittingSchedule) {
        onSuccessScheduling && onSuccessScheduling();
      } else {
        onSuccess && onSuccess(taskRequests);
      }
    } catch (e) {
      setSubmitting(false);

      if (submittingSchedule) {
        onFailScheduling && onFailScheduling(e as Error);
      } else {
        onFail && onFail(e as Error, taskRequests);
      }
    }
  };

  const handleSubmitNow: React.MouseEventHandler = async (ev) => {
    ev.preventDefault();
    await handleSubmit(false);
  };

  const handleSubmitSchedule: React.FormEventHandler = async (ev) => {
    ev.preventDefault();
    await handleSubmit(true);
  };

  const handleSubmitFavoriteTask: React.MouseEventHandler = async (ev) => {
    ev.preventDefault();
    if (!favoriteTaskBuffer.name) {
      setFavoriteTaskTitleError(true);
      return;
    }

    setFavoriteTaskTitleError(false);

    if (!submitFavoriteTask) {
      return;
    }

    try {
      setSavingFavoriteTask(true);
      await submitFavoriteTask(favoriteTaskBuffer);
      setSavingFavoriteTask(false);
      onSuccessFavoriteTask &&
        onSuccessFavoriteTask(
          `${!favoriteTaskBuffer.id ? `Created` : `Edited`} favorite task successfully`,
          favoriteTaskBuffer,
        );

      setOpenFavoriteDialog(false);

      setCallToUpdateFavoriteTask(false);
    } catch (e) {
      setSavingFavoriteTask(false);
      onFailFavoriteTask && onFailFavoriteTask(e as Error, favoriteTaskBuffer);
    }
  };

  const handleDeleteFavoriteTask: React.MouseEventHandler = async (ev) => {
    ev.preventDefault();

    if (!deleteFavoriteTask) {
      return;
    }

    try {
      setDeletingFavoriteTask(true);
      await deleteFavoriteTask(favoriteTaskBuffer);
      setDeletingFavoriteTask(false);
      onSuccessFavoriteTask &&
        onSuccessFavoriteTask('Deleted favorite task successfully', favoriteTaskBuffer);
      setTaskRequests([defaultTask()]);
      setOpenFavoriteDialog(false);
      setCallToDeleteFavoriteTask(false);
      setCallToUpdateFavoriteTask(false);
    } catch (e) {
      setDeletingFavoriteTask(false);
      onFailFavoriteTask && onFailFavoriteTask(e as Error, favoriteTaskBuffer);
    }
  };

  const handleSelectFileClick: React.MouseEventHandler<HTMLButtonElement> = () => {
    if (!tasksFromFile) {
      return;
    }

    (async () => {
      const newTasks = await tasksFromFile();

      if (newTasks.length === 0) {
        return;
      }

      setTaskRequests(newTasks);
      setSelectedTaskIdx(0);
    })();
  };

  const submitText = taskRequests.length > 1 ? 'Submit All Now' : 'Submit Now';

  return (
    <>
      <StyledDialog
        title="Create Task"
        maxWidth="lg"
        fullWidth={taskRequests.length > 1}
        disableEnforceFocus
        {...otherProps}
      >
        <form aria-label="create-task">
          <DialogTitle>
            <Grid container wrap="nowrap">
              <Grid item className={classes.title}>
                Create Task
              </Grid>

              <Grid item>
                <FormToolbar onSelectFileClick={handleSelectFileClick} />
              </Grid>
            </Grid>
          </DialogTitle>

          <DialogContent>
            <Grid container direction="row" wrap="nowrap">
              {SHOW_FAVORITES_UI && showFavorite && (
                <List dense className={classes.taskList} aria-label="Favorites Tasks">
                  <Typography variant="h6" component="div">
                    Favorite tasks
                  </Typography>

                  {favoritesTasks.map((favoriteTask, index) => {
                    return (
                      <FavoriteTask
                        listItemText={favoriteTask.name}
                        key={index}
                        setFavoriteTask={setFavoriteTaskBuffer}
                        favoriteTask={favoriteTask}
                        setCallToDelete={setCallToDeleteFavoriteTask}
                        setCallToUpdate={setCallToUpdateFavoriteTask}
                        setOpenDialog={setOpenFavoriteDialog}
                        listItemClick={() => {
                          setFavoriteTaskBuffer(favoriteTask);

                          setTaskRequests([
                            {
                              category: favoriteTask.category,
                              description: favoriteTask.description,
                              unix_millis_earliest_start_time: Date.now(),
                              priority: favoriteTask.priority,
                            },
                          ]);
                        }}
                      />
                    );
                  })}
                </List>
              )}

              {SHOW_FAVORITES_UI && showFavorite && (
                <Divider
                  orientation="vertical"
                  flexItem
                  style={{ marginLeft: theme.spacing(2), marginRight: theme.spacing(2) }}
                />
              )}

              <Grid item xs sx={{ minWidth: 0 }}>
                <Grid container spacing={theme.spacing(2)}>
                  <Grid item xs={12}>
                    <TextField
                      select
                      id="task-type"
                      label="Category"
                      variant="outlined"
                      fullWidth
                      margin="normal"
                      value={taskRequest.category}
                      onChange={handleTaskTypeChange}
                    >
                      <MenuItem value="patrol">Patrol</MenuItem>
                      <MenuItem value="clean">Clean</MenuItem>
                      <MenuItem value="delivery">Delivery</MenuItem>
                    </TextField>
                  </Grid>

                  <Grid item xs={12}>
                    <TextField
                      select
                      id="priority"
                      label="Priority"
                      fullWidth
                      value={getPriorityValue(taskRequest.priority)}
                      SelectProps={{
                        renderValue: (selected) => {
                          const option = getPriorityOption(Number(selected));

                          return (
                            <span
                              style={{
                                display: 'inline-flex',
                                alignItems: 'center',
                                gap: PRIORITY_LABEL_GAP,
                              }}
                            >
                              <span
                                style={{
                                  width: PRIORITY_DOT_SIZE,
                                  height: PRIORITY_DOT_SIZE,
                                  borderRadius: '50%',
                                  backgroundColor: option.color,
                                  display: 'inline-block',
                                }}
                              />

                              {option.label}
                            </span>
                          );
                        },
                      }}
                      onChange={(ev) => {
                        const value = Number(ev.target.value);
                        taskRequest.priority = { type: 'binary', value };
                        setFavoriteTaskBuffer({
                          ...favoriteTaskBuffer,
                          priority: { type: 'binary', value },
                        });
                        updateTasks();
                      }}
                    >
                      {PRIORITY_OPTIONS.map((option) => (
                        <MenuItem key={option.value} value={option.value}>
                          <span
                            style={{
                              display: 'inline-flex',
                              alignItems: 'center',
                              gap: PRIORITY_LABEL_GAP,
                            }}
                          >
                            <span
                              style={{
                                width: PRIORITY_DOT_SIZE,
                                height: PRIORITY_DOT_SIZE,
                                borderRadius: '50%',
                                backgroundColor: option.color,
                                display: 'inline-block',
                              }}
                            />
                            {option.label}
                          </span>
                        </MenuItem>
                      ))}
                    </TextField>
                  </Grid>
                </Grid>

                <Divider
                  orientation="horizontal"
                  flexItem
                  style={{ marginTop: theme.spacing(2), marginBottom: theme.spacing(2) }}
                />

                {renderTaskDescriptionForm()}

                {SHOW_FAVORITES_UI && (
                  <Grid container justifyContent="center">
                    <Button
                      aria-label="Save as a favorite task"
                      variant="contained"
                      color="primary"
                      onClick={() => {
                        !callToUpdateFavoriteTask &&
                          setFavoriteTaskBuffer({ ...favoriteTaskBuffer, name: '', id: '' });

                        setOpenFavoriteDialog(true);
                      }}
                      style={{ marginTop: theme.spacing(2), marginBottom: theme.spacing(2) }}
                    >
                      {callToUpdateFavoriteTask ? `Confirm edits` : 'Save as a favorite task'}
                    </Button>
                  </Grid>
                )}
              </Grid>

              {taskTitles.length > 1 && (
                <>
                  <Divider
                    orientation="vertical"
                    flexItem
                    style={{ marginLeft: theme.spacing(2), marginRight: theme.spacing(2) }}
                  />

                  <List dense className={classes.taskList} aria-label="Tasks List">
                    {taskTitles.map((title, idx) => (
                      <ListItem
                        key={idx}
                        button
                        onClick={() => setSelectedTaskIdx(idx)}
                        className={selectedTaskIdx === idx ? classes.selectedTask : undefined}
                        role="listitem button"
                      >
                        <ListItemText primary={title} />
                      </ListItem>
                    ))}
                  </List>
                </>
              )}
            </Grid>
          </DialogContent>

          <DialogActions>
            <Button
              variant="outlined"
              disabled={submitting}
              className={classes.actionBtn}
              onClick={() => onClose && onClose({} as never, 'escapeKeyDown')}
            >
              Cancel
            </Button>

            {!immediateMode && (
              <Button
                variant="contained"
                color="primary"
                disabled={submitting || !scheduleActionEnabled}
                className={classes.actionBtn}
                onClick={() => setOpenSchedulingDialog(true)}
              >
                {scheduleToEdit ? 'Edit schedule' : 'Add to Schedule'}
              </Button>
            )}

            <Button
              variant="contained"
              type="submit"
              color="primary"
              disabled={
                submitting || !formFullyFilled || (!immediateMode && scheduleToEdit !== undefined)
              }
              className={classes.actionBtn}
              aria-label={submitText}
              onClick={handleSubmitNow}
            >
              <Loading hideChildren loading={submitting} size="1.5em" color="inherit">
                {submitText}
              </Loading>
            </Button>
          </DialogActions>
        </form>
      </StyledDialog>

      {SHOW_FAVORITES_UI && openFavoriteDialog && (
        <ConfirmationDialog
          confirmText={callToDeleteFavoriteTask ? 'Delete' : 'Save'}
          cancelText="Back"
          open={openFavoriteDialog}
          title={callToDeleteFavoriteTask ? 'Confirm Delete' : 'Favorite Task'}
          submitting={callToDeleteFavoriteTask ? deletingFavoriteTask : savingFavoriteTask}
          onClose={() => {
            setOpenFavoriteDialog(false);

            setCallToDeleteFavoriteTask(false);
          }}
          onSubmit={callToDeleteFavoriteTask ? handleDeleteFavoriteTask : handleSubmitFavoriteTask}
        >
          {!callToDeleteFavoriteTask && (
            <TextField
              size="small"
              value={favoriteTaskBuffer.name}
              onChange={(e) =>
                setFavoriteTaskBuffer({ ...favoriteTaskBuffer, name: e.target.value })
              }
              helperText="Required"
              error={favoriteTaskTitleError}
            />
          )}

          {callToDeleteFavoriteTask && (
            <Typography>{`Are you sure you want to delete "${favoriteTaskBuffer.name}"?`}</Typography>
          )}
        </ConfirmationDialog>
      )}

      {!immediateMode && openSchedulingDialog && (
        <ConfirmationDialog
          confirmText="Schedule"
          cancelText="Cancel"
          open={openSchedulingDialog}
          title="Schedule Task"
          submitting={false}
          onClose={() => setOpenSchedulingDialog(false)}
          onSubmit={(ev: React.FormEvent) => {
            handleSubmitSchedule(ev);

            setOpenSchedulingDialog(false);
          }}
        >
          <Grid container spacing={theme.spacing(2)} marginTop={theme.spacing(1)}>
            <Grid item xs={12}>
              <DatePicker
                value={schedule.startOn}
                onChange={(date) =>
                  date &&
                  setSchedule((prev) => {
                    const nextStartOn = new Date(date.valueOf());
                    nextStartOn.setHours(prev.at.getHours());
                    nextStartOn.setMinutes(prev.at.getMinutes());
                    nextStartOn.setSeconds(0, 0);

                    const shouldSyncUntil =
                      !prev.recurring || !prev.until || isSameDay(prev.until, prev.startOn);

                    return {
                      ...prev,
                      startOn: nextStartOn,
                      days: prev.recurring ? prev.days : getRecurringDaysForDate(nextStartOn),
                      until: shouldSyncUntil ? endOfDay(nextStartOn) : prev.until,
                    };
                  })
                }
                label="Start On"
                disabled={!scheduleEnabled}
                renderInput={(props) => <TextField {...props} fullWidth />}
              />
            </Grid>

            <Grid item xs={isCleanTask ? 12 : 6}>
              <TimePicker
                minutesStep={1}
                value={schedule.at}
                onChange={(date) => {
                  if (!date) {
                    return;
                  }

                  setSchedule((prev) => ({ ...prev, at: date }));

                  if (!isNaN(date.valueOf())) {
                    setSchedule((prev) => {
                      const startOn = prev.startOn;

                      startOn.setHours(date.getHours());

                      startOn.setMinutes(date.getMinutes());

                      return { ...prev, startOn };
                    });
                  }
                }}
                label="At"
                disabled={!scheduleEnabled}
                renderInput={(props) => <TextField {...props} fullWidth />}
              />
            </Grid>
            {!isCleanTask && (
              <Grid item xs={6}>
                <TimePicker
                  minutesStep={1}
                  value={schedule.plannedEndAt ?? defaultPlannedEnd(schedule.at)}
                  onChange={(date) => {
                    if (!date) {
                      return;
                    }

                    setSchedule((prev) => ({ ...prev, plannedEndAt: date }));
                  }}
                  label="Planned end"
                  disabled={!scheduleEnabled}
                  renderInput={(props) => <TextField {...props} fullWidth />}
                />
              </Grid>
            )}

            <Grid item xs={12}>
              <FormControl fullWidth>
                <FormHelperText>Schedule type</FormHelperText>

                <RadioGroup
                  row
                  value={
                    schedule.recurring ? ScheduleTypeValue.RECURRING : ScheduleTypeValue.ONE_TIME
                  }
                  onChange={(event: React.ChangeEvent<HTMLInputElement>) => {
                    const recurring = event.target.value === ScheduleTypeValue.RECURRING;

                    setSchedule((prev) => ({
                      ...prev,

                      recurring,

                      days:
                        recurring && !hasSelectedRecurringDay(prev.days)
                          ? getRecurringDaysForDate(prev.startOn)
                          : prev.days,

                      until: recurring
                        ? scheduleUntilValue === ScheduleUntilValue.ON
                          ? prev.until ?? endOfDay(prev.startOn)
                          : undefined
                        : endOfDay(prev.startOn),
                    }));

                    if (!recurring) {
                      setScheduleUntilValue(ScheduleUntilValue.ON);
                    }
                  }}
                >
                  <FormControlLabel
                    value={ScheduleTypeValue.ONE_TIME}
                    control={<Radio />}
                    disabled={!scheduleEnabled}
                    label="One-time"
                  />

                  <FormControlLabel
                    value={ScheduleTypeValue.RECURRING}
                    control={<Radio />}
                    disabled={!scheduleEnabled}
                    label="Recurring"
                  />
                </RadioGroup>
              </FormControl>
            </Grid>

            {schedule.recurring && (
              <Grid item xs={12}>
                <FormHelperText>Repeat on</FormHelperText>

                <DaySelectorSwitch
                  value={schedule.days}
                  disabled={!scheduleEnabled}
                  onChange={(days) => setSchedule((prev) => ({ ...prev, days }))}
                />
              </Grid>
            )}
          </Grid>

          <Grid container marginTop={theme.spacing(1)} marginLeft={theme.spacing(0)}>
            <FormControl fullWidth={true}>
              <FormHelperText>
                {schedule.recurring ? 'Recurring ends' : 'One-time run date'}
              </FormHelperText>

              <RadioGroup
                aria-labelledby="controlled-radio-buttons-group"
                name="controlled-radio-buttons-group"
                value={schedule.recurring ? scheduleUntilValue : ScheduleUntilValue.ON}
                onChange={handleScheduleUntilValue}
                row
              >
                <Grid item xs={6} paddingLeft={theme.spacing(1)}>
                  <FormControlLabel
                    value={ScheduleUntilValue.NEVER}
                    control={<Radio />}
                    disabled={!scheduleEnabled || !schedule.recurring}
                    label="Never"
                  />
                </Grid>

                <Grid item xs={2} paddingLeft={theme.spacing(1)}>
                  <FormControlLabel
                    value={ScheduleUntilValue.ON}
                    control={<Radio />}
                    disabled={!scheduleEnabled || !schedule.recurring}
                    label="On"
                  />
                </Grid>

                <Grid item xs={4}>
                  <DatePicker
                    value={schedule.until ?? endOfDay(schedule.startOn)}
                    onChange={(date) =>
                      date &&
                      setSchedule((prev) => {
                        return { ...prev, until: endOfDay(date) };
                      })
                    }
                    disabled={
                      !scheduleEnabled ||
                      !schedule.recurring ||
                      scheduleUntilValue !== ScheduleUntilValue.ON
                    }
                    renderInput={(props) => <TextField {...props} fullWidth />}
                  />
                </Grid>
              </RadioGroup>
            </FormControl>
          </Grid>
        </ConfirmationDialog>
      )}
    </>
  );
}
