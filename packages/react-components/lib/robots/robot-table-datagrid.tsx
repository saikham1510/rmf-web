import {
  DataGrid,
  GridColDef,
  GridEventListener,
  GridValueGetterParams,
  MuiEvent,
  GridRowParams,
  GridCellParams,
} from '@mui/x-data-grid';
import { Box, SxProps, Typography, useTheme } from '@mui/material';
import * as React from 'react';
import { ApiServerModelsRmfApiRobotStateStatus as RobotStatus } from 'api-client';
import { RobotTableData } from './robot-table';
import { robotStatusToUpperCase } from './utils';

export interface RobotDataGridTableProps {
  onRobotClick?(ev: MuiEvent<React.MouseEvent<HTMLElement>>, robotName: RobotTableData): void;
  robots: RobotTableData[];
}

export function RobotDataGridTable({ onRobotClick, robots }: RobotDataGridTableProps): JSX.Element {
  const handleEvent: GridEventListener<'rowClick'> = (
    params: GridRowParams,
    event: MuiEvent<React.MouseEvent<HTMLElement>>,
  ) => {
    if (onRobotClick) {
      onRobotClick(event, params.row);
    }
  };

  const Status = (params: GridCellParams): React.ReactNode => {
    const theme = useTheme();
    const statusLabelStyle: SxProps = (() => {
      const error = {
        color: theme.palette.error.main,
      };
      const charging = {
        color: theme.palette.info.main,
      };
      const working = {
        color: theme.palette.success.main,
      };
      const defaultColor = {
        color: theme.palette.warning.main,
      };

      switch (params.row.status) {
        case RobotStatus.Error:
          return error;
        case RobotStatus.Charging:
          return charging;
        case RobotStatus.Working:
          return working;
        default:
          return defaultColor;
      }
    })();

    return (
      <Box component="div" sx={statusLabelStyle}>
        <Typography
          data-testid="status"
          component="p"
          sx={{
            fontWeight: 'bold',
            fontSize: 14,
          }}
        >
          {robotStatusToUpperCase(params.row.status)}
        </Typography>
      </Box>
    );
  };

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
      if (timeOffsetMs !== 0) setTimeOffsetMs(0);
      return;
    }
    if (timeOffsetMs === null) {
      const simNowMs = maxRaw * 1000;
      setTimeOffsetMs(Date.now() - simNowMs);
    }
  }, [robots, timeOffsetMs]);

  const effectiveTimeOffsetMs = timeOffsetMs ?? 0;

  const toDate = React.useCallback(
    (ts?: number): Date | null => {
      if (!ts && ts !== 0) return null;
      const isMillis = ts > 1e12;
      const asMs = isMillis ? ts : ts * 1000;
      return new Date(isMillis ? asMs : asMs + effectiveTimeOffsetMs);
    },
    [effectiveTimeOffsetMs],
  );

  const columns: GridColDef[] = [
    {
      field: 'name',
      headerName: 'Name',
      width: 150,
      editable: false,
      valueGetter: (params: GridValueGetterParams) => params.row.name,
      flex: 1,
      filterable: true,
    },
    {
      field: 'fleet',
      headerName: 'Fleet',
      width: 90,
      valueGetter: (params: GridValueGetterParams) => params.row.fleet,
      flex: 1,
      filterable: true,
    },
    {
      field: 'estFinishTime',
      headerName: 'Est. Task Finish Time',
      width: 150,
      editable: false,
      valueGetter: (params: GridValueGetterParams) => {
        const ts = params.row.estFinishTime;
        if (ts === undefined || ts === null) return '-';
        const d = toDate(ts);
        return d ? d.toLocaleString() : '-';
      },
      flex: 1,
      filterable: true,
    },
    {
      field: 'level',
      headerName: 'Level',
      width: 150,
      editable: false,
      valueGetter: (params: GridValueGetterParams) => params.row.level,
      flex: 1,
      filterable: true,
    },
    {
      field: 'battery',
      headerName: 'Battery',
      width: 150,
      editable: false,
      valueGetter: (params: GridValueGetterParams) => `${params.row.battery * 100}%`,
      flex: 1,
      filterable: true,
    },
    {
      field: 'lastUpdateTime',
      headerName: 'Last Updated',
      width: 150,
      editable: false,
      valueGetter: (params: GridValueGetterParams) => {
        const ts = params.row.lastUpdateTime;
        if (ts === undefined || ts === null) return '-';
        const d = toDate(ts);
        return d ? d.toLocaleString() : '-';
      },
      flex: 1,
      filterable: true,
    },
    {
      field: 'status',
      headerName: 'Status',
      editable: false,
      flex: 1,
      renderCell: Status,
      filterable: true,
    },
  ];

  return (
    <DataGrid
      autoHeight={true}
      getRowId={(r) => r.name}
      rows={robots}
      pageSize={5}
      rowHeight={38}
      columns={columns}
      rowsPerPageOptions={[5]}
      onRowClick={handleEvent}
      initialState={{
        sorting: {
          sortModel: [{ field: 'name', sort: 'asc' }],
        },
      }}
      disableVirtualization={true}
    />
  );
}
