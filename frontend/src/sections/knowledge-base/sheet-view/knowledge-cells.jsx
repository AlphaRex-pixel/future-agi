import { Box, Stack, Typography } from "@mui/material";
import PropTypes from "prop-types";
import React from "react";
import Iconify from "src/components/iconify";
import CustomTooltip from "src/components/tooltip/CustomTooltip";
import { getFileIcon, statusIcons } from "./icons";

export function TitleCell(props) {
  const { value } = props;
  const fileType = value.split(".").pop();
  const iconSrc = getFileIcon(fileType);

  return (
    <Stack
      direction={"row"}
      gap={"8px"}
      alignItems={"center"}
      sx={{
        height: "100%",
      }}
    >
      <Box
        component={"img"}
        sx={{
          height: "16px",
          width: "16px",
        }}
        alt="document icon"
        src={iconSrc}
      />
      <Typography fontWeight={"fontWeightRegular"} variant="s1">
        {value}
      </Typography>
    </Stack>
  );
}

TitleCell.propTypes = {
  value: PropTypes.string,
};

const cellStyles = {
  Processing: {
    backgroundColor: "blue.o10",
    color: "blue.500",
  },
  Failed: {
    backgroundColor: "red.o10",
    color: "red.500",
  },
  Completed: {
    backgroundColor: "green.o10",
    color: "green.500",
  },
};

export function ProcessingStatusCell(props) {
  const { value, data } = props;

  const statusIconSrc = statusIcons[value];

  return (
    <Box
      sx={{
        height: "100%",
        width: "fit-content",
        display: "flex",
        justifyContent: "flex-start",
        alignItems: "center",
      }}
    >
      <Stack
        sx={{
          px: "12px",
          py: "4px",
          gap: "8px",
          height: "24px",
          borderRadius: "20px",
          ...cellStyles[value],
        }}
        direction={"row"}
        alignItems={"center"}
      >
        <Box
          component={"img"}
          sx={{
            height: "12px",
            width: "12px",
          }}
          alt="document icon"
          src={statusIconSrc}
        />
        <Typography variant="s2" fontWeight={"fontWeightMedium"}>
          {value}
        </Typography>
        {value === "Failed" && data?.error && (
          <CustomTooltip show={true} title={data.error} arrow>
            <Iconify
              icon="solar:info-circle-outline"
              color="text.primary"
              width={12}
              height={12}
            />
          </CustomTooltip>
        )}
      </Stack>
    </Box>
  );
}

ProcessingStatusCell.propTypes = {
  value: PropTypes.string,
  data: PropTypes.object,
};
