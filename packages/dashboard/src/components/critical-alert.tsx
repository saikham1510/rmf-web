import React from 'react';
import {
  Card,
  CardContent,
  CardHeader,
  IconButton,
  Box,
  Typography,
  Button,
  Stack,
} from '@mui/material';
import CloseIcon from '@mui/icons-material/Close';
import { base } from 'react-components';

export interface CriticalAlertProps {
  id: string;
  message: string;
  category: string;
  severity: string;
  timestamp: number;
  robot_name?: string;
  nearest_waypoint?: string;
  x?: number;
  y?: number;
  onAcknowledge: (id: string) => void;
}

export const CriticalAlert = React.memo((props: CriticalAlertProps) => {
  const {
    id,
    message,
    category,
    severity,
    timestamp,
    robot_name,
    nearest_waypoint,
    x,
    y,
    onAcknowledge,
  } = props;

  const isFire = category.includes('fire');
  const accentColor = isFire ? '#ff0000' : '#ff9800';

  const timeString = new Date(timestamp).toLocaleTimeString();

  return (
    <Card
      sx={{
        position: 'fixed',
        bottom: '20px',
        right: '20px',
        width: '350px',
        backgroundColor: base.palette.background.paper,
        borderLeft: `5px solid ${accentColor}`,
        boxShadow: `0 0 15px ${accentColor}33, ${base.shadows[8]}`,
        zIndex: 9999,
        animation: 'slideInRight 0.3s ease-out',
        '@keyframes slideInRight': {
          from: {
            transform: 'translateX(400px)',
            opacity: 0,
          },
          to: {
            transform: 'translateX(0)',
            opacity: 1,
          },
        },
      }}
    >
      <CardHeader
        avatar={
          <Box
            component="div"
            sx={
              {
                fontSize: '1.8rem',
                display: 'flex',
                alignItems: 'center',
              } as const
            }
          >
            {isFire ? 'FIRE' : 'ALERT'}
          </Box>
        }
        action={
          <IconButton size="small" onClick={() => onAcknowledge(id)} sx={{ color: accentColor }}>
            <CloseIcon />
          </IconButton>
        }
        title={
          <Typography
            variant="subtitle1"
            sx={{
              fontWeight: 600,
              color: accentColor,
            }}
          >
            {isFire ? 'FIRE ALERT' : 'CRITICAL ALERT'}
          </Typography>
        }
        subheader={
          <Typography
            variant="caption"
            sx={{
              color: base.palette.text.secondary,
            }}
          >
            {timeString}
          </Typography>
        }
      />

      <CardContent
        sx={{
          pt: 0,
        }}
      >
        {/* Main message */}
        <Typography
          variant="body2"
          sx={{
            mb: 1.5,
            color: base.palette.text.primary,
            fontWeight: 500,
          }}
        >
          {message}
        </Typography>

        {/* Location details */}
        <Box
          component="div"
          sx={
            {
              backgroundColor: base.palette.action.hover,
              p: 1,
              borderRadius: 1,
              mb: 1.5,
            } as const
          }
        >
          {robot_name && (
            <Typography variant="caption" sx={{ display: 'block', mb: 0.5 }}>
              <strong>Robot:</strong> {robot_name}
            </Typography>
          )}
          {nearest_waypoint && (
            <Typography variant="caption" sx={{ display: 'block', mb: 0.5 }}>
              <strong>Location:</strong> {nearest_waypoint}
            </Typography>
          )}
          {x !== undefined && y !== undefined && (
            <Typography variant="caption" sx={{ display: 'block' }}>
              <strong>Coordinates:</strong> ({x.toFixed(1)}, {y.toFixed(1)})
            </Typography>
          )}
        </Box>

        {/* Action buttons */}
        <Stack direction="row" spacing={1}>
          <Button
            variant="contained"
            size="small"
            onClick={() => onAcknowledge(id)}
            sx={{
              backgroundColor: accentColor,
              color: '#fff',
              flex: 1,
              '&:hover': {
                backgroundColor: accentColor,
                opacity: 0.9,
              },
            }}
          >
            Acknowledge
          </Button>
          <Button
            variant="outlined"
            size="small"
            sx={{
              flex: 1,
              borderColor: accentColor,
              color: accentColor,
              '&:hover': {
                borderColor: accentColor,
                backgroundColor: `${accentColor}11`,
              },
            }}
          >
            Details
          </Button>
        </Stack>
      </CardContent>
    </Card>
  );
});

CriticalAlert.displayName = 'CriticalAlert';
