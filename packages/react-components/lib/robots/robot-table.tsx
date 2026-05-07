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
  // Keep sim "now" aligned with wall-clock "now" even if simulation runs faster/slower.
  // Only use second-based values for offset calculation to avoid mixing with epoch ms fields.
  const effectiveTimeOffsetMs = React.useMemo(() => {
    const simSecondTs: number[] = [];
    for (const r of robots) {
      if (
        r.estFinishTime !== undefined &&
        r.estFinishTime !== null &&
        r.estFinishTime > 0 &&
        r.estFinishTime < 1e12
      ) {
        simSecondTs.push(r.estFinishTime);
      }
      if (
        r.lastUpdateTime !== undefined &&
        r.lastUpdateTime !== null &&
        r.lastUpdateTime > 0 &&
        r.lastUpdateTime < 1e12
      ) {
        simSecondTs.push(r.lastUpdateTime);
      }
    }
    if (simSecondTs.length === 0) return 0;
    const simNowMs = Math.max(...simSecondTs) * 1000;
    return Date.now() - simNowMs;
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
