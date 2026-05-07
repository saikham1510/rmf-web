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
  // Keep sim "now" aligned with wall-clock "now" while preventing stale fleet
  // updates from pulling the inferred sim clock backward.
  const latestSimSecondMsRef = React.useRef<number>(0);

  const effectiveTimeOffsetMs = React.useMemo(() => {
    let latestSimSecondMs = latestSimSecondMsRef.current;

    for (const r of robots) {
      const candidateTimes = [r.estFinishTime, r.lastUpdateTime];
      for (const candidate of candidateTimes) {
        if (candidate !== undefined && candidate !== null && candidate > 0 && candidate < 1e12) {
          latestSimSecondMs = Math.max(latestSimSecondMs, candidate * 1000);
        }
      }
    }

    latestSimSecondMsRef.current = latestSimSecondMs;
    return latestSimSecondMs > 0 ? Date.now() - latestSimSecondMs : 0;
  }, [robots]);
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
            timeOffsetMs={effectiveTimeOffsetMs}
            onClick={(ev) => onRobotClick && onRobotClick(ev, robot)}
          />
        ))}
      </TableBody>
    </Table>
  );
}
