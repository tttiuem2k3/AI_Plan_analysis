from sqlalchemy.orm import Session
from ..repositories.ke_hoach_repo import KeHoachRepo
from ..schemas.ke_hoach_schema import KeHoachSXCreate, KeHoachSXUpdate

class KeHoachService:
    @staticmethod
    def get_all(db: Session, skip: int = 0, limit: int = 100, search: str = None):
        return KeHoachRepo.get_all(db, skip, limit, search)

    @staticmethod
    def get_by_id(db: Session, ke_hoach_id: int):
        return KeHoachRepo.get_by_id(db, ke_hoach_id)

    @staticmethod
    def create(db: Session, obj_in: KeHoachSXCreate):
        return KeHoachRepo.create(db, obj_in)

    @staticmethod
    def update(db: Session, ke_hoach_id: int, obj_in: KeHoachSXUpdate):
        return KeHoachRepo.update(db, ke_hoach_id, obj_in)

    @staticmethod
    def delete(db: Session, ke_hoach_id: int):
        # Xóa kế hoạch + chi tiết, và trả trạng thái đơn hàng liên quan về 'Chưa lập kế hoạch'
        return KeHoachRepo.delete_plan_and_reset_orders(db, ke_hoach_id)

    @staticmethod
    def get_chitiet_by_kehoach(db: Session, ke_hoach_id: int):
        return KeHoachRepo.get_chitiet_by_kehoach(db, ke_hoach_id)

    @staticmethod
    def get_btp_grouped(db: Session, ke_hoach_id: int):
        return KeHoachRepo.get_btp_grouped(db, ke_hoach_id)

    @staticmethod
    def get_calendar_view(db, ke_hoach_id: int):
        return KeHoachRepo.get_calendar_view(db, ke_hoach_id)

    @staticmethod
    def get_late_alerts(db: Session):
        return KeHoachRepo.get_late_alerts(db)
