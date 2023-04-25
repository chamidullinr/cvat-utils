import io
import logging
import os
import time
import warnings
import zipfile
from typing import Dict, List, Tuple, Union

from cvat_utils import api_requests, config
from cvat_utils.models import (
    Frame,
    FullAnnotations,
    FullProject,
    FullTask,
    FullTaskMetadata,
    Job,
    Task,
)
from cvat_utils.utils import is_image

logger = logging.getLogger("cvat_utils")


def _load_project(project_id: int) -> FullProject:
    project_url = f"{config.API_URL}/projects/{project_id}"
    project_dict = api_requests.get(project_url)
    project = FullProject(**project_dict)
    if project.dict() != project_dict:
        warnings.warn(
            "Project model in the library doesn't equal to the model returned by CVAT API."
        )
    return project


def _load_task(task_id: int) -> FullTask:
    task_url = f"{config.API_URL}/tasks/{task_id}"
    task_dict = api_requests.get(task_url)
    task = FullTask(**task_dict)
    if task.dict() != task_dict:
        warnings.warn("Task model in the library doesn't equal to the model returned by CVAT API.")
    return task


def _load_task_metadata(task_id: int) -> FullTaskMetadata:
    task_meta_url = f"{config.API_URL}/tasks/{task_id}/data/meta"
    meta_dict = api_requests.get(task_meta_url)
    meta = FullTaskMetadata(**meta_dict)
    if meta.dict() != meta_dict:
        warnings.warn(
            "Task Metadata model in the library doesn't equal to the model returned by CVAT API."
        )
    return meta


def load_project_data(
    project_id: int, *, return_dict: bool = False
) -> Tuple[Union[FullProject, dict], List[Union[Task, dict]]]:
    """Load project metadata from CVAT."""
    project = _load_project(project_id)
    tasks = [
        Task(**_load_task(task_id).dict()) for task_id in project.tasks
    ]  # get list of tasks in the project
    if return_dict:
        project = project.dict()
        tasks = [x.dict() for x in tasks]
    return project, tasks


def image_path_to_image_id(image_path: str) -> str:
    """Create image ID from image path in stored CVAT."""
    return ".".join(image_path.split(".")[:-1])


def load_task_data(
    task_id: int, *, return_dict: bool = False
) -> Tuple[Union[FullTask, dict], List[Union[Job, dict]], Dict[int, Union[Frame, dict]]]:
    """Load task metadata from CVAT."""
    # load annotation data from CVAT
    task = _load_task(task_id)
    meta = _load_task_metadata(task_id)

    # get list of jobs in the task
    assert all(
        [len(x.jobs) == 1 for x in task.segments]
    ), "Unexpected CVAT data: one segment has multiple jobs."
    jobs = [
        Job(**job.dict(), start_frame=segment.start_frame, stop_frame=segment.stop_frame)
        for segment in task.segments
        for job in segment.jobs
    ]

    # get list of frames
    frame_ids_range = range(meta.start_frame, meta.stop_frame + 1)
    frames = {
        frame_id: Frame(  # frame id should be unique in the current task only
            id=image_path_to_image_id(x.name),  # id should be unique across the whole dataset
            frame_id=frame_id,
            file_name=x.name.split("/")[-1],
            width=x.width,
            height=x.height,
            task_id=task_id,
            task_name=task.name,
        )
        for frame_id, x in zip(frame_ids_range, meta.frames)
    }

    # add job ids to the frames
    for job_data in jobs:
        for frame_id in range(job_data.start_frame, job_data.stop_frame + 1):
            assert (
                frame_id in frames
            ), f"Unexpected CVAT data: job ({job_data.id}) is missing a frame ({frame_id})."
            frames[frame_id].job_id = job_data.id
            frames[frame_id].status = job_data.status
    for frame_id, frame_data in frames.items():
        assert (
            frame_data.job_id is not None
        ), f"Unexpected CVAT data: frame ({frame_id}) is missing job id."

    if return_dict:
        task = task.dict()
        jobs = [x.dict() for x in jobs]
        frames = {k: v.dict() for k, v in frames.items()}

    return task, jobs, frames


def load_annotations(
    job: Union[Job, dict], *, return_dict: bool = False
) -> Union[FullAnnotations, dict]:
    """Load annotations from a single job in CVAT."""
    if isinstance(job, Job):
        job_url = job.url
    else:
        assert "url" in job
        job_url = job["url"]
    if job_url[-1] == "/":
        job_url = job_url[:-1]
    annotations_dict = api_requests.get(f"{job_url}/annotations")
    annotations = FullAnnotations(**annotations_dict)
    if annotations.dict() != annotations_dict:
        warnings.warn(
            "Annotations model in the library doesn't equal to the model returned by CVAT API."
        )
    if return_dict:
        annotations = annotations.dict()
    return annotations


def download_images(task_id: int, output_path: str, keep_image_path: bool = True) -> List[str]:
    """Download images from CVAT and save them to a local directory.

    Parameters
    ----------
    task_id
        CVAT Task ID to load images from.
    output_path
        A local directory where images will be saved.
    keep_image_path
        If False download images into a sup-directories named based on task IDs.
        Otherwise, download images from different tasks into the same directory structure.

    Returns
    -------
    A list of downloaded images.
    """
    url = f"{config.API_URL}/tasks/{task_id}/dataset"

    # create request and wait till 201 (created) status code
    while True:
        resp = api_requests.get(url, params={"format": "CVAT for images 1.1"}, load_content=False)
        if resp.status_code == 201:
            break
        if resp.status_code == 500:
            logger.error(f"Error: receiver response 500 with content: {resp.content}")
        time.sleep(5)

    # load images
    # send GET request as a stream and check content length in headers first
    # then decide if to download directly to memory or by chunks to a file
    resp = api_requests.get(
        url,
        params={
            "format": "CVAT for images 1.1",
            "action": "download",
        },
        load_content=False,
        stream=True,
    )
    content_legth_gb = float(resp.headers["Content-Length"]) * 1e-9
    keep_in_memory = content_legth_gb <= config.DOWNLOAD_THRESHOLD_GB
    if keep_in_memory:
        # download data directly to memory
        buffer_or_file = io.BytesIO(resp.content)
    else:
        # download data by chunks to a file
        resp.raise_for_status()
        buffer_or_file = os.path.join(output_path, f"task-{task_id}.zip")
        os.makedirs(output_path, exist_ok=True)
        with open(buffer_or_file, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)

    # extract zip file
    if keep_image_path:
        output_task_path = output_path
        os.makedirs(output_task_path, exist_ok=True)
    else:
        output_task_path = os.path.join(output_path, f"task-{task_id}")
        os.makedirs(output_task_path, exist_ok=False)
    with zipfile.ZipFile(buffer_or_file) as zip_ref:
        files = [x for x in zip_ref.namelist() if is_image(x)]
        zip_ref.extractall(output_task_path, members=files)
    if not keep_image_path:
        files = [os.path.join(f"task-{task_id}", x) for x in files]
    logger.debug(f"Downloaded and extracted {len(files)} files to '{output_task_path}'.")

    return files
