"""The main runtime of the MythX CLI."""

import logging
import sys
import time
from glob import glob
from pathlib import Path

import click

from mythx_cli import __version__
from mythx_cli.formatter import FORMAT_RESOLVER
from mythx_cli.payload import (
    generate_bytecode_payload,
    generate_solidity_payload,
    generate_truffle_payload,
)
from mythx_models.response import (
    AnalysisListResponse,
    GroupCreationResponse,
    GroupListResponse,
)
from pythx import Client, MythXAPIError
from pythx.middleware.group_data import GroupDataMiddleware
from pythx.middleware.toolname import ClientToolNameMiddleware

LOGGER = logging.getLogger("mythx-cli")
logging.basicConfig(level=logging.WARNING)


@click.group()
@click.option(
    "--debug/--no-debug",
    default=False,
    show_default=True,
    envvar="MYTHX_DEBUG",
    help="Provide additional debug output",
)
@click.option(
    "--access-token",
    envvar="MYTHX_ACCESS_TOKEN",
    help="Your access token generated from the MythX dashboard",
)
@click.option(
    "--eth-address",
    envvar="MYTHX_ETH_ADDRESS",
    help="Your MythX account's Ethereum address",
)
@click.option(
    "--password",
    envvar="MYTHX_PASSWORD",
    help="Your MythX account's password as set in the dashboard",
)
@click.option(
    "--staging/--production",
    default=False,
    hidden=True,
    envvar="MYTHX_STAGING",
    help="Use the MythX staging environment",
)
@click.option(
    "--format",
    "fmt",
    default="table",
    type=click.Choice(FORMAT_RESOLVER.keys()),
    show_default=True,
    help="The format to display the results in",
)
@click.pass_context
def cli(ctx, **kwargs):
    """Your CLI for interacting with https://mythx.io/
    \f

    :param ctx: Click context holding group-level parameters
    :param debug: Boolean to enable the `logging` debug mode
    :param access_token: User JWT access token from the MythX dashboard
    :param eth_address: The MythX account ETH address/username
    :param password: The account password from the MythX dashboard
    :param staging: Boolean to redirect requests to MythX staging
    :param fmt: The formatter to use for the subcommand output
    """

    ctx.obj = dict(kwargs)
    toolname_mw = ClientToolNameMiddleware(name="mythx-cli-{}".format(__version__))
    if kwargs["access_token"] is not None:
        ctx.obj["client"] = Client(
            access_token=kwargs["access_token"],
            staging=kwargs["staging"],
            middlewares=[toolname_mw],
        )
    elif kwargs["eth_address"] and kwargs["password"]:
        ctx.obj["client"] = Client(
            eth_address=kwargs["eth_address"],
            password=kwargs["password"],
            staging=kwargs["staging"],
            middlewares=[toolname_mw],
        )
    else:
        # default to trial user client
        ctx.obj["client"] = Client(
            eth_address="0x0000000000000000000000000000000000000000",
            password="trial",
            staging=kwargs["staging"],
            middlewares=[toolname_mw],
        )
    if kwargs["debug"]:
        for name in logging.root.manager.loggerDict:
            logging.getLogger(name).setLevel(logging.DEBUG)

    return 0


@cli.group()
def group():
    """Create, modify, and view analysis groups."""
    pass


def find_truffle_artifacts(project_dir):
    """Look for a Truffle build folder and return all relevant JSON artifacts.

    This function will skip the Migrations.json file and return all other files
    under :code:`<project-dir>/build/contracts/`. If no files were found,
    :code:`None` is returned.

    :param project_dir: The base directory of the Truffle project
    :return: Files under :code:`<project-dir>/build/contracts/` or :code:`None`
    """

    output_pattern = Path(project_dir) / "build" / "contracts" / "*.json"
    artifact_files = list(glob(str(output_pattern.absolute())))
    if not artifact_files:
        return None

    return [f for f in artifact_files if not f.endswith("Migrations.json")]


def find_solidity_files(project_dir):
    """Return all Solidity files in the given directory.

    This will match all files with the `.sol` extension.

    :param project_dir: The directory to search in
    :return: Solidity files in `project_dir` or `None`
    """

    output_pattern = Path(project_dir) / "*.sol"
    artifact_files = list(glob(str(output_pattern.absolute())))
    if not artifact_files:
        return None

    return artifact_files


@cli.command()
@click.argument(
    "target", default=None, nargs=-1, required=False  # allow multiple targets
)
@click.option(
    "--async/--wait",  # TODO: make default on full
    "async_flag",
    help="Submit the job and print the UUID, or wait for execution to finish",
)
@click.option(
    "--mode", type=click.Choice(["quick", "full"]), default="quick", show_default=True
)
@click.option(
    "--group-id",
    type=click.STRING,
    help="The group ID to add the analysis to",
    default=None,
)
@click.option(
    "--group-name",
    type=click.STRING,
    help="The group name to attach to the analysis",
    default=None,
)
@click.pass_obj
def analyze(ctx, target, async_flag, mode, group_id, group_name):
    """Analyze the given directory or arguments with MythX.
    \f

    :param ctx: Click context holding group-level parameters
    :param target: Arguments passed to the `analyze` subcommand
    :param async_flag: Whether to execute the analysis asynchronously
    :param mode: Full or quick analysis mode
    :param group_id: The group ID to add the analysis to
    :param group_name: The group name to attach to the analysis
    :return:
    """

    if group_id or group_name:
        group_mw = GroupDataMiddleware(group_id=group_id, group_name=group_name)
        ctx["client"].handler.middlewares.append(group_mw)

    jobs = []

    if not target:
        if Path("truffle-config.js").exists() or Path("truffle.js").exists():
            files = find_truffle_artifacts(Path.cwd())
            if not files:
                raise click.exceptions.UsageError(
                    (
                        "Could not find any truffle artifacts. Are you in the project root? "
                        "Did you run truffle compile?"
                    )
                )
            LOGGER.debug(
                "Detected Truffle project with files:\n{}".format("\n".join(files))
            )
            for file in files:
                jobs.append(generate_truffle_payload(file))

        elif list(glob("*.sol")):
            files = find_solidity_files(Path.cwd())
            click.confirm(
                "Do you really want to submit {} Solidity files?".format(len(files))
            )
            LOGGER.debug("Found Solidity files to submit:\n{}".format("\n".join(files)))
            for file in files:
                jobs.append(generate_solidity_payload(file))
        else:
            raise click.exceptions.UsageError(
                "No argument given and unable to detect Truffle project or Solidity files"
            )
    else:
        for target_elem in target:
            if target_elem.startswith("0x"):
                LOGGER.debug("Identified target {} as bytecode".format(target_elem))
                jobs.append(generate_bytecode_payload(target_elem))
                continue
            elif Path(target_elem).is_file() and Path(target_elem).suffix == ".sol":
                LOGGER.debug(
                    "Trying to interpret {} as a solidity file".format(target_elem)
                )
                jobs.append(generate_solidity_payload(target_elem))
                continue
            else:
                raise click.exceptions.UsageError(
                    "Could not interpret argument {} as bytecode or Solidity file".format(
                        target_elem
                    )
                )

    uuids = []
    with click.progressbar(jobs) as bar:
        for job in bar:
            # attach execution mode, submit, poll
            job.update({"analysis_mode": mode})
            resp = ctx["client"].analyze(**job)
            uuids.append(resp.uuid)

    if async_flag:
        click.echo("\n".join(uuids))
        return

    for uuid in uuids:
        while not ctx["client"].analysis_ready(uuid):
            # TODO: Add poll interval option
            time.sleep(3)
        resp = ctx["client"].report(uuid)
        inp = ctx["client"].request_by_uuid(uuid)
        ctx["uuid"] = uuid
        click.echo(FORMAT_RESOLVER[ctx["fmt"]].format_detected_issues(resp, inp))


@cli.command()
@click.argument("uuids", default=None, nargs=-1)
@click.pass_obj
def status(ctx, uuids):
    """Get the status of an already submitted analysis.
    \f

    :param ctx: Click context holding group-level parameters
    :param uuids: A list of job UUIDs to fetch the status for
    """
    for uuid in uuids:
        resp = ctx["client"].status(uuid)
        click.echo(FORMAT_RESOLVER[ctx["fmt"]].format_analysis_status(resp))


@group.command("list")
@click.option(
    "--number",
    default=5,
    type=click.IntRange(min=1, max=100),  # ~ 5 requests à 20 entries
    show_default=True,
    help="The number of most recent groups to display",
)
@click.pass_obj
def group_list(ctx, number):
    """Get a list of analysis groups.
    \f

    :param ctx: Click context holding group-level parameters
    :param number: The number of analysis groups to display
    :return:
    """

    client: Client = ctx["client"]
    result = GroupListResponse(groups=[], total=0)
    try:
        offset = 0
        while True:
            resp = client.group_list(offset=offset)
            if not resp.groups:
                break
            offset += len(resp.groups)
            result.groups.extend(resp.groups)
            if len(result.groups) >= number:
                break

        # trim result to desired result number
        LOGGER.debug(resp.total)
        result = GroupListResponse(groups=result[:number], total=resp.total)
    except MythXAPIError:
        raise click.UsageError(
            (
                "This functionality is only available to registered users. "
                "Head over to https://mythx.io/ and register a free account to "
                "list your past analyses. Alternatively, you can look up the "
                "status of a specific job by calling 'mythx status <uuid>'."
            )
        )
    click.echo(FORMAT_RESOLVER[ctx["fmt"]].format_group_list(result))


@group.command("status")
@click.argument("gids", default=None, nargs=-1)
@click.pass_obj
def group_status(ctx, gids):
    """Get the status of an analysis group.
    \f

    :param ctx: Click context holding group-level parameters
    :param gids: A list of group IDs to fetch the status for
    """

    for gid in gids:
        resp = ctx["client"].group_status(group_id=gid)
        click.echo(FORMAT_RESOLVER[ctx["fmt"]].format_group_status(resp))


@group.command("open")
@click.argument("name", default="", nargs=1)
@click.pass_obj
def group_open(ctx, name):
    """Create a new group to assign future analyses to.
    \f

    :param ctx: Click context holding group-level parameters
    :param name: The name of the group to be created (autogenerated if empty)
    """

    resp: GroupCreationResponse = ctx["client"].create_group(group_name=name)
    click.echo(
        "Opened group with ID {} and name '{}'".format(
            resp.group.identifier, resp.group.name
        )
    )


@group.command("close")
@click.argument("identifiers", nargs=-1, required=True)
@click.pass_obj
def group_close(ctx, identifiers):
    """Close/seal an existing group to prevent future analyses from being added.
    \f

    :param ctx: Click context holding group-level parameters
    :param identifiers: The group ID(s) to seal
    """

    for identifier in identifiers:
        resp: GroupCreationResponse = ctx["client"].seal_group(group_id=identifier)
        click.echo(
            "Closed group with ID {} and name '{}'".format(
                resp.group.identifier, resp.group.name
            )
        )


@cli.command(name="list")
@click.option(
    "--number",
    default=5,
    type=click.IntRange(min=1, max=100),  # ~ 5 requests à 20 entries
    show_default=True,
    help="The number of most recent analysis jobs to display",
)
@click.pass_obj
def list_(ctx, number):
    """Get a list of submitted analyses.
    \f

    :param ctx: Click context holding group-level parameters
    :param number: The number of analysis jobs to display
    :return:
    """

    result = AnalysisListResponse(analyses=[], total=0)
    try:
        offset = 0
        while True:
            resp = ctx["client"].analysis_list(offset=offset)
            if not resp.analyses:
                break
            offset += len(resp.analyses)
            result.analyses.extend(resp.analyses)
            if len(result.analyses) >= number:
                break

        # trim result to desired result number
        LOGGER.debug(resp.total)
        result = AnalysisListResponse(analyses=result[:number], total=resp.total)
    except MythXAPIError:
        raise click.UsageError(
            (
                "This functionality is only available to registered users. "
                "Head over to https://mythx.io/ and register a free account to "
                "list your past analyses. Alternatively, you can look up the "
                "status of a specific job by calling 'mythx status <uuid>'."
            )
        )
    click.echo(FORMAT_RESOLVER[ctx["fmt"]].format_analysis_list(result))


@cli.command()
@click.argument("uuids", default=None, nargs=-1)
@click.pass_obj
def report(ctx, uuids):
    """Fetch the report for a single or multiple job UUIDs.
    \f

    :param ctx: Click context holding group-level parameters
    :param uuids: List of UUIDs to display the report for
    :return:
    """

    for uuid in uuids:
        resp = ctx["client"].report(uuid)
        inp = ctx["client"].request_by_uuid(uuid)
        ctx["uuid"] = uuid
        click.echo(FORMAT_RESOLVER[ctx["fmt"]].format_detected_issues(resp, inp))


@cli.command()
@click.pass_obj
def version(ctx):
    """Display API version information.
    \f

    :param ctx: Click context holding group-level parameters
    :return:
    """

    resp = ctx["client"].version()
    click.echo(FORMAT_RESOLVER[ctx["fmt"]].format_version(resp))


if __name__ == "__main__":
    sys.exit(cli())  # pragma: no cover
