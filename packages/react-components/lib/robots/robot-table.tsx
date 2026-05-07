import {
  SxProps,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableProps,
  TableRow,
  useTheme,
} from '@mui/material';
import type { RobotState } from 'api-client';
import React from 'react';

type RobotStatus = Required<RobotState>['status'];

export interface RobotTableData {
  fleet: string;
  name: string;
  status?: RobotStatus;
  battery?: number;
  estFinishTime?: number;
  lastUpdateTime?: number;
  level?: string;
}

interface RobotRowProps extends RobotTableData {
  onClick: React.MouseEventHandler<HTMLTableRowElement>;
  timeOffsetMs?: number;
}

const RobotRow = React.memo(
  ({
    fleet,
    name,
    status,
    battery = 0,
    estFinishTime,
    lastUpdateTime,
    onClick,
    timeOffsetMs = 0,
  }: RobotRowProps) => {
    const theme = useTheme();

    const toDate = React.useCallback(
      (ts?: number): Date | null => {
        if (!ts && ts !== 0) return null;
        const isMillis = ts > 1e12;
        const asMs = isMillis ? ts : ts * 1000;
        return new Date(isMillis ? asMs : asMs + timeOffsetMs);
      },
      [timeOffsetMs],
    );

    const robotStatusClass: SxProps = React.useMemo(() => {
      if (!status) {
        return {};
      }
      switch (status) {
        case 'error':
          return {
            backgroundColor: theme.palette.error.main,
          };
        case 'charging':
          return {
            backgroundColor: theme.palette.info.main,
          };
        case 'working':
          return {
            backgroundColor: theme.palette.success.main,
          };
        case 'idle':
        case 'offline':
        case 'shutdown':
        case 'uninitialized':
          return {
            backgroundColor: theme.palette.warning.main,
          };
      }
    }, [status, theme]);

    return (
      <TableRow
        onClick={onClick}
        sx={{
          cursor: 'pointer',
          backgroundColor: theme.palette.action.hover,
        }}
      >
        <TableCell>{fleet}</TableCell>
        <TableCell>{name}</TableCell>
        <TableCell>
          {(() => {
            if (estFinishTime === undefined || estFinishTime === null) return '-';
            const d = toDate(estFinishTime);
            return d ? d.toLocaleString() : '-';
          })()}
        </TableCell>
        <TableCell>{(battery * 100).toFixed(2)}%</TableCell>
        <TableCell>
          {(() => {
            if (lastUpdateTime === undefined || lastUpdateTime === null) return '-';
            const d = toDate(lastUpdateTime);
            return d ? d.toLocaleString() : '-';
          })()}
        </TableCell>
        <TableCell sx={robotStatusClass}>{status}</TableCell>
      </TableRow>
    );
  },
);

export interface RobotTableProps extends TableProps {
  /**
   * The current list of robots to display, when pagination is enabled, this should only
   * contain the robots for the current page.
   */
  robots: RobotTableData[];
  onRobotClick?(ev: React.MouseEvent<HTMLDivElement>, robotName: RobotTableData): void;
}

export function RobotTable({ robots, onRobotClick, ...otherProps }: RobotTableProps): JSX.Element {
  // Compute a stable offset that maps simulation timestamps to real time.
  // Initialize offset once when we first see small (second) timestamps and keep it
  // so values do not jump when data updates frequently. If we later detect the
  // incoming timestamps are real epoch milliseconds, reset offset to zero.
  const [timeOffsetMs, setTimeOffsetMs] = React.useState<number | null>(null);

  React.useEffect(() => {
    const rawTs: number[] = [];
    for (const r of robots) {
      if (r.estFinishTime !== undefined && r.estFinishTime !== null) rawTs.push(r.estFinishTime);
      if (r.lastUpdateTime !== undefined && r.lastUpdateTime !== null) rawTs.push(r.lastUpdateTime);
    }
    if (rawTs.length === 0) return;
    const maxRaw = Math.max(...rawTs);
    if (maxRaw > 1e12) {
      // timestamps already in ms - ensure offset is zero
      if (timeOffsetMs !== 0) setTimeOffsetMs(0);
      return;
    }
    // Only initialize offset once to avoid flicker. Use the first observed sim "now".
    if (timeOffsetMs === null) {
      const simNowMs = maxRaw * 1000;
      setTimeOffsetMs(Date.now() - simNowMs);
    }
  }, [robots, timeOffsetMs]);

  // Use 0 if offset still uninitialized
  const effectiveTimeOffsetMs = timeOffsetMs ?? 0;
  return (
    <Table stickyHeader size="small" style={{ tableLayout: 'fixed' }} {...otherProps}>
      <TableHead>
        <TableRow>
          <TableCell>Fleet</TableCell>
          <TableCell>Robot Name</TableCell>
          <TableCell>Est. Task Finish Time</TableCell>
          <TableCell>Battery</TableCell>
          <TableCell>Last Updated</TableCell>
          <TableCell>Status</TableCell>
        </TableRow>
      </TableHead>
      <TableBody>
        {robots.map((robot, robot_id) => (
          <RobotRow
            key={robot_id}
            {...robot}
            timeOffsetMs={timeOffsetMs ?? undefined}
            onClick={(ev) => onRobotClick && onRobotClick(ev, robot)}
          />
        ))}
      </TableBody>
    </Table>
  );
}
