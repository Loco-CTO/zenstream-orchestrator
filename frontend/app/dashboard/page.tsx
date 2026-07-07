"use client";

import React, { useCallback, useState } from "react";
import { IconHome, IconKey } from "@tabler/icons-react";
import {
  Gadget,
  Greeting,
  Codeblock,
  Button,
  Input,
} from "./components/page_util";
import generateInvite from "./modules/invites";
import appConfig from "../config";

/**
 * Dashboard component that displays the main dashboard page.
 * @returns A React element containing the dashboard page.
 */
export default function Dashboard() {
  const [invite, setInvite] = useState<string>("ur mother");
  // Add state for the API URL input value
  const [apiUrlInput, setApiUrlInput] = useState<string>(appConfig.apiUrl);
  // Optional state for tracking update status
  const [updateStatus, setUpdateStatus] = useState<string>("");

  /**
   * Handles the generation of a new invite link.
   */
  const handleGenerateInvite = useCallback(async () => {
    try {
      const newInvite = await generateInvite.generateInvite();
      setInvite(`http://127.0.0.1:3000/auth/register/${newInvite}`);
    } catch (error) {
      console.error("Error generating invite:", error);
    }
  }, []);

  /**
   * Handles updating the API URL config.
   */
  const handleUpdateApiUrl = useCallback((): Promise<void> => {
    try {
      appConfig.updateApiUrl(apiUrlInput);
      setUpdateStatus("URL updated successfully!");
      setTimeout(() => setUpdateStatus(""), 3000);
      return Promise.resolve();
    } catch (error) {
      console.error("Error updating URL:", error);
      setUpdateStatus("Failed to update URL");
      setTimeout(() => setUpdateStatus(""), 3000);
      return Promise.reject(new Error("Failed to update URL"));
    }
  }, [apiUrlInput]);

  return (
    <div className="w-full h-screen bg-schemes-dark-surface-container-lowest items-start justify-start">
      <Greeting user="user" />
      <div className="grid grid-cols-1 md:grid-cols-2 items-start justify-center pt-16">
        <div className="flex flex-col items-start justify-start w-full h-full gap-9 singlecol-padding md:leftcol-padding">
          <Gadget
            title="Generate Orchestrator Invite"
            icon={<IconKey className="size-5 text-schemes-dark-on-background" />}
            content={
              <form
                method="POST"
                className="h-full w-full flex flex-col items-start justify-start gap-4 py-6"
              >
                <Codeblock code={invite} />
                <Button
                  label="Generate"
                  buttontype="submit"
                  onClick={handleGenerateInvite}
                />
              </form>
            }
          />
          <Gadget
            title="Backend Address"
            icon={
              <IconHome className="size-5 text-schemes-dark-on-background" />
            }
            content={
              <div className="h-full w-full flex flex-col items-start justify-start gap-4 py-6">
                <Input
                  value={apiUrlInput}
                  onChange={(e) => setApiUrlInput(e.target.value)}
                />

                <div className="flex-col w-full items-center justify-between">
                  <Button
                    label="Update"
                    buttontype="button"
                    onClick={handleUpdateApiUrl}
                  />
                  {updateStatus && (
                    <span className="text-schemes-dark-on-background text-md pl-3">
                      {updateStatus}
                    </span>
                  )}
                </div>
              </div>
            }
          />
        </div>
        <div className="flex flex-col items-start justify-start w-full h-full gap-9 singlecol-padding md:rightcol-padding">
          <div className="gadget-desktop h-64" />
          <div className="gadget-desktop h-96" />
          <div className="gadget-desktop h-96" />
        </div>
      </div>
    </div>
  );
}
