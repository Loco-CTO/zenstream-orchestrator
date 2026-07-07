"use client";

import React, { ReactElement, useCallback } from "react";
import { IconCopy } from "@tabler/icons-react";

/**
 * This file contains various reusable components for the dashboard page.
 * Components included:
 * - Greeting: Displays a welcome message.
 * - Gadget: Displays a gadget with a title, icon, and content.
 * - Codeblock: Displays a block of code with a copy button.
 * - Checkbox: Displays a checkbox with a label.
 * - Button: Displays a button with a label.
 */

/**
 * Greeting component that displays a welcome message.
 * @param user - The name of the user to greet.
 * @returns A React element containing the greeting message.
 */
function Greeting({ user }: { user: string }) {
  return (
    <h1 className="text-4xl text-schemes-dark-on-background w-full pl-16 pt-16 font-sans font-semibold">
      Welcome back, {user}
    </h1>
  );
}

/**
 * Gadget component that displays a title, icon, and content.
 * @param title - The title of the gadget.
 * @param icon - The icon to display next to the title.
 * @param content - The content to display inside the gadget.
 * @returns A React element containing the gadget.
 */
function Gadget({
  title,
  icon,
  content,
}: {
  title: string;
  icon: ReactElement;
  content: React.ReactElement;
}) {
  return (
    <div className="flex gadget-desktop w-full h-auto flex-col">
      <div className="flex w-full h-auto flex-row justify-start items-center gap-3">
        <div className="pt-6 pl-8">{icon}</div>
        <div className="text-xl w-auto pt-6 font-sans font-medium text-schemes-dark-on-background">
          {title}
        </div>
      </div>
      <div className="h-auto w-full flex flex-col items-start justify-start gap-4 px-6">
        {content}
      </div>
    </div>
  );
}

/**
 * Codeblock component that displays a block of code with a copy button.
 * @param code - The code to display inside the code block.
 * @returns A React element containing the code block.
 */
function Codeblock({ code }: { code: string }) {
  return (
    <div className="flex flex-row h-12 w-full bg-schemes-dark-surface-dim shadow-inner shadow-schemes-dark-surface-container-lowest rounded-xl">
      <pre className="flex grow items-center px-4 overflow-x-scroll">
        <code className="text-schemes-dark-on-background bg-opacity-0 font-mono font-normal text-md whitespace-nowrap pt-1">
          {code}
        </code>
      </pre>
      <button
        type="button"
        className="flex items-center justify-center size-12 cursor-pointer"
      >
        <IconCopy className="size-5 text-schemes-dark-on-background bg-opacity-0" />
      </button>
    </div>
  );
}

/**
 * Checkbox component that displays a checkbox with a label.
 * @param label - The label to display next to the checkbox.
 * @returns A React element containing the checkbox and label.
 */
function Checkbox({ label }: { label: string }) {
  return (
    <div className="flex flex-row items-center justify-start gap-2 mr-6 mb-4">
      <input
        type="checkbox"
        className="appearance-none w-6 h-6 border-2 border-transparent hover:border-opacity-100 hover:border-schemes-dark-primary shadow-inner shadow-schemes-dark-surface-container-lowest bg-schemes-dark-surface-dim rounded-md checked:shadow-none checked:bg-schemes-dark-primary transition-all duration-100"
      />
      <label className="text-schemes-dark-on-background font-sans font-medium text-lg w-28">
        {label}
      </label>
    </div>
  );
}

/**
 * Button component that displays a button with a label.
 * @param label - The label to display on the button.
 * @param buttontype - The type of the button (button, submit, or reset).
 * @returns A React element containing the button.
 */
interface ButtonProps {
  label: string;
  buttontype: "button" | "submit" | "reset" | undefined;
  onClick?: () => Promise<void>;
}

function Button({ label, buttontype, onClick }: ButtonProps) {
  /**
   * Handles the click event for the button.
   * Prevents the default form submission behavior and performs a custom action.
   * Memoized to prevent unnecessary re-renders.
   *
   * @param event - The mouse event triggered by clicking the button.
   */
  const handleClick = useCallback(
    async (event: React.MouseEvent<HTMLButtonElement>) => {
      event.preventDefault();
      if (onClick) {
        await onClick();
      }
    },
    [onClick],
  );

  return (
    <button
      type={buttontype}
      className="px-4 w-auto h-12 bg-schemes-dark-primary text-schemes-dark-on-primary font-sans font-medium text-lg rounded-lg shadow-schemes-dark-surface-container-lowest hover:shadow-none hover:bg-schemes-dark-primary-hover transition-all duration-100 cursor-pointer"
      onClick={handleClick}
    >
      {label}
    </button>
  );
}

function Input({
  value,
  onChange,
}: {
  value: string;
  onChange?: (event: React.ChangeEvent<HTMLInputElement>) => void;
}) {
  return (
    <div className="flex flex-row h-12 w-full bg-schemes-dark-surface-dim shadow-inner shadow-schemes-dark-surface-container-lowest rounded-xl">
      <input
        type="text"
        value={value}
        onChange={onChange}
        className="flex text-schemes-dark-on-background bg-opacity-0 font-mono font-normal text-md whitespace-nowrap px-3 w-full h-full"
      />
    </div>
  );
}

export { Greeting, Gadget, Codeblock, Checkbox, Button, Input };
